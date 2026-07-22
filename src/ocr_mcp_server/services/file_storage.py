"""Handle-anchored, server-named storage for validated incoming files.

``data_root`` is the trust anchor and must be writable only by the service
account. POSIX descendants are opened component-by-component with ``dir_fd``
and ``O_NOFOLLOW``. Windows keeps directory and staged-file handles open,
publishes through ``SetFileInformationByHandle``, and fails safely whenever
the configured pathname no longer identifies the held directory.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import errno
import hashlib
import io
import os
from pathlib import Path
import stat
import sys
import threading
from typing import BinaryIO
from uuid import UUID, uuid4, uuid5

from ..domain.constants import SUPPORTED_EXTENSIONS
from ..domain.errors import FileIntakeErrorCode, FileIntakeFailure
from ..domain.files import IncomingFile, StoredFile
from .file_validation import FileValidator, ValidatedFileMetadata


@dataclass(frozen=True, slots=True)
class BatchUsage:
    file_count: int
    total_bytes: int


@dataclass(slots=True)
class BatchLockLease:
    """Held batch lock whose one-byte marker can durably retire the batch."""

    descriptor: int
    verify_identity: Callable[[], bool]

    def retire(self) -> None:
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        os.write(self.descriptor, b"\x01")
        os.ftruncate(self.descriptor, 1)
        os.fsync(self.descriptor)
        if not self.verify_identity():
            raise OSError("batch lock identity changed")


@dataclass(frozen=True, slots=True)
class _OpenedDirectory:
    path: Path
    descriptor: int | None
    windows_handle: int | None
    identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _DerivativeOutcome:
    stored: StoredFile
    directory: _OpenedDirectory
    descriptor: int
    identity: tuple[int, int]
    name: str


class _BoundedWriter:
    """Seek-compatible binary writer that rejects growth before it happens."""

    def __init__(self, raw: BinaryIO, limit: int) -> None:
        self._raw = raw
        self._limit = limit
        self._extent = 0

    def write(self, data) -> int:
        try:
            view = memoryview(data)
        except TypeError:
            raise TypeError("a bytes-like object is required") from None
        if not view.c_contiguous:
            raise TypeError("non-contiguous writes are not supported")
        byte_view = view.cast("B")
        size = byte_view.nbytes
        end = self.tell() + size
        if max(self._extent, end) > self._limit:
            raise FileIntakeFailure(FileIntakeErrorCode.TOO_LARGE)
        written = self._raw.write(byte_view)
        if written is None or written < 0 or written > size:
            raise OSError("invalid write result")
        self._extent = max(self._extent, self.tell())
        return written

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._raw.seek(offset, whence)

    def tell(self) -> int:
        return self._raw.tell()

    def truncate(self, size: int | None = None) -> int:
        target = self.tell() if size is None else size
        if type(target) is not int or target < 0 or target > self._limit:
            raise FileIntakeFailure(FileIntakeErrorCode.TOO_LARGE)
        result = self._raw.truncate(target)
        self._extent = target
        return result

    def flush(self) -> None:
        self._raw.flush()

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def fileno(self) -> int:
        raise io.UnsupportedOperation("descriptor access is not available")

    @property
    def closed(self) -> bool:
        return self._raw.closed



class FileStorage:
    """Persist files beneath ``data_root/<batch UUID>/input`` only."""

    def __init__(
        self,
        data_root: Path,
        *,
        id_factory: Callable[[], object] | None = None,
    ) -> None:
        self._data_root = Path(os.path.abspath(data_root))
        self._id_factory = id_factory

    @asynccontextmanager
    async def batch_lock(
        self,
        batch_id: str,
        *,
        marker_registry,
        allow_missing_marker: bool = False,
        allow_retired: bool = False,
    ):
        """Hold a process-shared exclusive lock for one canonical batch UUID."""

        canonical_batch_id = self._canonical_uuid(batch_id)
        root: _OpenedDirectory | None = None
        lock_dir: _OpenedDirectory | None = None
        descriptor: int | None = None
        acquired = False
        primary: BaseException | None = None
        cleanup_failed = False
        try:
            root = self._open_data_root(create=True)
            if root is None:
                raise OSError("data root unavailable")
            lock_dir = self._open_child_directory(root, ".locks", create=True)
            if lock_dir is None:
                raise OSError("lock directory unavailable")
            descriptor, created = self._open_lock_file(
                lock_dir, f"{canonical_batch_id}.lock"
            )
            lock_name = f"{canonical_batch_id}.lock"
            lock_identity = self._identity(os.fstat(descriptor))

            def lock_unchanged() -> bool:
                try:
                    if root is None or lock_dir is None:
                        return False
                    info = (
                        os.stat(
                            lock_name,
                            dir_fd=lock_dir.descriptor,
                            follow_symlinks=False,
                        )
                        if lock_dir.descriptor is not None
                        else os.lstat(lock_dir.path / lock_name)
                    )
                    return (
                        self._directory_unchanged(root)
                        and self._directory_unchanged(lock_dir)
                        and not self._is_reparse(info)
                        and stat.S_ISREG(info.st_mode)
                        and self._identity(info) == lock_identity
                    )
                except OSError:
                    return False

            if not lock_unchanged():
                raise OSError("unsafe batch lock")

            def read_locked_marker() -> bytes:
                info = os.fstat(descriptor)
                if (
                    self._is_reparse(info)
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or self._identity(info) != lock_identity
                    or not lock_unchanged()
                ):
                    raise OSError("unsafe batch lock")
                os.lseek(descriptor, 0, os.SEEK_SET)
                marker = os.read(descriptor, 2)
                after = os.fstat(descriptor)
                if (
                    self._is_reparse(after)
                    or not stat.S_ISREG(after.st_mode)
                    or after.st_nlink != 1
                    or self._identity(after) != lock_identity
                    or after.st_size != len(marker)
                    or not lock_unchanged()
                ):
                    raise OSError("unsafe batch lock")
                return marker

            while not acquired:
                acquired = self._try_batch_lock(descriptor)
                if not acquired:
                    await asyncio.sleep(0.01)
            if not lock_unchanged():
                raise OSError("unsafe batch lock")
            marker = read_locked_marker()
            if created:
                if marker == b"":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    self._write_all(descriptor, b"\0")
                    os.fsync(descriptor)
                    marker = read_locked_marker()
                elif marker != b"\x00":
                    raise OSError("invalid new batch lock")
            elif marker == b"":
                def initialize_empty_marker() -> None:
                    if read_locked_marker() != b"":
                        raise OSError("empty batch lock changed")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    self._write_all(descriptor, b"\0")
                    os.fsync(descriptor)
                    if read_locked_marker() != b"\x00":
                        raise OSError("batch lock initialization failed")

                await marker_registry.bind_empty_lock_marker(
                    canonical_batch_id,
                    lock_identity,
                    allow_missing=allow_missing_marker,
                    initialize=initialize_empty_marker,
                )
                marker = read_locked_marker()
                if marker != b"\x00":
                    raise OSError("batch lock changed during initialization")

            recover_unbound = not created and marker == b"\x00"
            await marker_registry.bind_lock_marker(
                canonical_batch_id,
                lock_identity,
                created=created,
                recover_unbound=recover_unbound,
                allow_missing=allow_missing_marker,
            )
            marker = read_locked_marker()
            if (created or recover_unbound) and marker != b"\x00":
                raise OSError("batch lock changed during binding")
            if marker == b"\x01":
                if not allow_retired:
                    raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
            elif marker != b"\x00":
                raise OSError("invalid batch lock marker")
            yield BatchLockLease(descriptor, lock_unchanged)
            if not lock_unchanged():
                raise OSError("unsafe batch lock")
        except FileIntakeFailure as exc:
            primary = exc
        except OSError:
            primary = FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        except BaseException as exc:
            primary = exc

        if descriptor is not None and acquired:
            try:
                self._release_batch_lock(descriptor)
            except OSError:
                cleanup_failed = True
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                cleanup_failed = True
        self._close_directory(lock_dir)
        self._close_directory(root)

        if primary is not None:
            if isinstance(primary, FileIntakeFailure):
                self._clear_exception_context(primary)
            raise primary
        if cleanup_failed:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)

    async def store(
        self,
        batch_id: str,
        incoming: IncomingFile,
        *,
        max_file_size_bytes: int,
        validator: FileValidator,
    ) -> StoredFile:
        directory: _OpenedDirectory | None = None
        descriptor: int | None = None
        staged_identity: tuple[int, int] | None = None
        part_name = f".{uuid4()}.part"
        published = False
        result: StoredFile | None = None
        failure: BaseException | None = None
        digest = hashlib.sha256()
        size_bytes = 0

        try:
            directory = self._open_input_directory(batch_id, create=True)
            if directory is None:
                raise OSError("input directory unavailable")
            descriptor = self._open_part(directory, part_name)
            staged_identity = self._identity(os.fstat(descriptor))

            async for chunk in incoming.content:
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise FileIntakeFailure(FileIntakeErrorCode.INVALID_DOCUMENT)
                if not chunk:
                    continue
                size_bytes += len(chunk)
                if size_bytes > max_file_size_bytes:
                    raise FileIntakeFailure(FileIntakeErrorCode.TOO_LARGE)
                self._write_all(descriptor, chunk)
                digest.update(chunk)
            os.fsync(descriptor)

            metadata = validator.validate(
                lambda: self._duplicate_reader(descriptor),
                display_name=incoming.display_name,
                declared_mime=incoming.declared_mime,
            )
            sha256 = digest.hexdigest()
            file_id = (
                str(uuid5(UUID(batch_id), sha256))
                if self._id_factory is None
                else self._canonical_uuid(str(self._id_factory()))
            )

            duplicate = self._find_duplicate(
                directory,
                sha256=sha256,
                size_bytes=size_bytes,
                metadata=metadata,
            )
            if duplicate is not None:
                result = duplicate
            else:
                target_name = f"{file_id}{metadata.extension}"
                self._assert_staged_identity(
                    directory, part_name, descriptor, staged_identity
                )
                self._publish_no_replace(
                    directory,
                    part_name,
                    target_name,
                    descriptor=descriptor,
                )
                published = True
                if not self._directory_unchanged(directory):
                    self._unlink_name(
                        directory,
                        target_name,
                        descriptor=descriptor if os.name == "nt" else None,
                        missing_ok=True,
                    )
                    published = False
                    raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
                self._assert_published_identity(
                    directory, target_name, descriptor, staged_identity
                )
                result = StoredFile(
                    file_id=file_id,
                    path=directory.path / target_name,
                    sha256=sha256,
                    size_bytes=size_bytes,
                    media_type=metadata.media_type,
                    extension=metadata.extension,
                    page_count=metadata.page_count,
                    width=metadata.width,
                    height=metadata.height,
                )
        except FileIntakeFailure as exc:
            failure = exc
        except Exception:
            failure = FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)

        cleanup_failed = False
        if descriptor is not None and not published and staged_identity is not None:
            cleanup_failed = not self._cleanup_staged_name(
                directory,
                part_name,
                staged_identity,
                descriptor=descriptor,
            )
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                cleanup_failed = True
        self._close_directory(directory)

        if failure is not None:
            if isinstance(failure, FileIntakeFailure):
                self._clear_exception_context(failure)
            raise failure
        if cleanup_failed or result is None:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        return result

    async def create_immutable_derivative(
        self,
        batch_id: str,
        source_file_id: str,
        extension: str,
        *,
        transform: Callable[[BinaryIO, BinaryIO], None],
        max_file_size_bytes: int,
        validator: FileValidator,
    ) -> StoredFile:
        """Transform a held input into a new server-named sibling atomically.

        Both names are derived solely from canonical UUIDs and a supported
        extension.  The callback receives duplicate handles, never paths.
        """

        if type(max_file_size_bytes) is not int or max_file_size_bytes < 1:
            raise FileIntakeFailure(FileIntakeErrorCode.TOO_LARGE)
        cancelled = threading.Event()
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._create_immutable_derivative_sync,
                batch_id,
                source_file_id,
                extension,
                transform,
                max_file_size_bytes,
                validator,
                cancelled,
            )
        )
        try:
            outcome = await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled.set()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    cancelled.set()
                except BaseException:
                    break
            outcome = None
            if not worker.cancelled():
                try:
                    outcome = worker.result()
                except BaseException:
                    pass
            if isinstance(outcome, _DerivativeOutcome):
                rollback = asyncio.create_task(
                    asyncio.to_thread(
                        self._scrub_immutable_derivative_sync,
                        batch_id,
                        outcome,
                    )
                )
                while not rollback.done():
                    try:
                        await asyncio.shield(rollback)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                try:
                    rollback.result()
                except BaseException:
                    raise FileIntakeFailure(
                        FileIntakeErrorCode.UNSAFE_PATH
                    ) from None
            raise
        # This is the commit linearization point.  It deliberately contains no
        # await: only held-descriptor/name verification and handle closure are
        # performed, so cancellation lands either before it (rollback above)
        # or after the successfully returned result.
        self._commit_immutable_derivative_sync(outcome)
        return outcome.stored

    def _create_immutable_derivative_sync(
        self,
        batch_id: str,
        source_file_id: str,
        extension: str,
        transform: Callable[[BinaryIO, BinaryIO], None],
        max_file_size_bytes: int,
        validator: FileValidator,
        cancelled: threading.Event,
    ) -> _DerivativeOutcome:
        canonical_batch_id = self._canonical_uuid(batch_id)
        canonical_source_id = self._canonical_uuid(source_file_id)
        normalized_extension = extension.lower() if isinstance(extension, str) else ""
        if normalized_extension not in SUPPORTED_EXTENSIONS:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSUPPORTED_TYPE)
        target_id = self._canonical_uuid(
            str(uuid4() if self._id_factory is None else self._id_factory())
        )
        if target_id == canonical_source_id:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)

        directory: _OpenedDirectory | None = None
        source_descriptor: int | None = None
        staged_descriptor: int | None = None
        staged_identity: tuple[int, int] | None = None
        part_name = f".{uuid4()}.part"
        target_name = f"{target_id}{normalized_extension}"
        source_name = f"{canonical_source_id}{normalized_extension}"
        published = False
        result: StoredFile | None = None
        failure: BaseException | None = None
        try:
            directory = self._open_input_directory(canonical_batch_id, create=False)
            if directory is None:
                raise OSError("input directory unavailable")
            source_descriptor = self._open_existing_file(directory, source_name)
            source_info = os.fstat(source_descriptor)
            source_identity = self._identity(source_info)
            if (
                self._is_reparse(source_info)
                or not stat.S_ISREG(source_info.st_mode)
                or source_info.st_nlink != 1
            ):
                raise OSError("unsafe source")
            self._assert_name_identity(directory, source_name, source_identity)
            source_digest = self._hash_descriptor(source_descriptor)

            staged_descriptor = self._open_part(directory, part_name)
            staged_identity = self._identity(os.fstat(staged_descriptor))
            with self._duplicate_reader(source_descriptor) as reader, os.fdopen(
                os.dup(staged_descriptor), "wb", closefd=True
            ) as raw_writer:
                writer = _BoundedWriter(raw_writer, max_file_size_bytes)
                transform(reader, writer)  # type: ignore[arg-type]
                writer.flush()
            if cancelled.is_set():
                raise asyncio.CancelledError
            output_info = os.fstat(staged_descriptor)
            if (
                self._is_reparse(output_info)
                or not stat.S_ISREG(output_info.st_mode)
                or output_info.st_nlink != 1
                or self._identity(output_info) != staged_identity
                or output_info.st_size < 1
                or output_info.st_size > max_file_size_bytes
            ):
                raise FileIntakeFailure(FileIntakeErrorCode.TOO_LARGE)
            os.fsync(staged_descriptor)

            mime = {
                ".pdf": "application/pdf",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
            }[normalized_extension]
            metadata = validator.validate(
                lambda: self._duplicate_reader(staged_descriptor),
                display_name=target_name,
                declared_mime=mime,
            )
            if cancelled.is_set():
                raise asyncio.CancelledError
            current_source = os.fstat(source_descriptor)
            if (
                self._is_reparse(current_source)
                or not stat.S_ISREG(current_source.st_mode)
                or current_source.st_nlink != 1
                or self._identity(current_source) != source_identity
                or current_source.st_size != source_info.st_size
                or self._hash_descriptor(source_descriptor) != source_digest
            ):
                raise OSError("source changed")
            self._assert_name_identity(directory, source_name, source_identity)
            self._assert_staged_identity(
                directory, part_name, staged_descriptor, staged_identity
            )
            before_publish = os.fstat(staged_descriptor)
            if (
                self._is_reparse(before_publish)
                or not stat.S_ISREG(before_publish.st_mode)
                or before_publish.st_nlink != 1
                or self._identity(before_publish) != staged_identity
                or before_publish.st_size != output_info.st_size
            ):
                raise OSError("unsafe staged output")
            if cancelled.is_set():
                raise asyncio.CancelledError
            self._publish_no_replace(
                directory,
                part_name,
                target_name,
                descriptor=staged_descriptor,
            )
            published = True
            if not self._directory_unchanged(directory):
                raise OSError("input directory changed")
            self._assert_published_identity(
                directory, target_name, staged_descriptor, staged_identity
            )
            published_info = os.fstat(staged_descriptor)
            if (
                self._is_reparse(published_info)
                or not stat.S_ISREG(published_info.st_mode)
                or published_info.st_nlink != 1
                or self._identity(published_info) != staged_identity
                or published_info.st_size != output_info.st_size
            ):
                raise OSError("unsafe published output")
            if cancelled.is_set():
                raise asyncio.CancelledError
            result = StoredFile(
                file_id=target_id,
                path=directory.path / target_name,
                sha256=self._hash_descriptor(staged_descriptor),
                size_bytes=output_info.st_size,
                media_type=metadata.media_type,
                extension=metadata.extension,
                page_count=metadata.page_count,
                width=metadata.width,
                height=metadata.height,
            )
        except BaseException as exc:
            if isinstance(exc, FileIntakeFailure) or not isinstance(exc, Exception):
                failure = exc
            else:
                failure = FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)

        cleanup_failed = False
        if source_descriptor is not None:
            try:
                os.close(source_descriptor)
            except OSError:
                cleanup_failed = True
        if cleanup_failed and failure is None:
            failure = FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        if failure is not None and staged_descriptor is not None:
            scrubbed = False
            for _ in range(2):
                try:
                    os.ftruncate(staged_descriptor, 0)
                    os.fsync(staged_descriptor)
                    scrubbed = True
                    break
                except OSError:
                    continue
            cleanup_failed = not scrubbed or cleanup_failed
        if failure is not None and staged_descriptor is not None and staged_identity is not None:
            cleanup_name = target_name if published else part_name
            cleanup_failed = (
                not self._cleanup_staged_name(
                    directory,
                    cleanup_name,
                    staged_identity,
                    descriptor=staged_descriptor,
                )
                or cleanup_failed
            )

        if failure is not None:
            if staged_descriptor is not None:
                try:
                    os.close(staged_descriptor)
                except OSError:
                    cleanup_failed = True
            self._close_directory(directory)
            if isinstance(failure, FileIntakeFailure):
                self._clear_exception_context(failure)
            raise failure
        if cleanup_failed or result is None or directory is None or staged_descriptor is None:
            if staged_descriptor is not None:
                try:
                    os.close(staged_descriptor)
                except OSError:
                    pass
            self._close_directory(directory)
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        return _DerivativeOutcome(
            result,
            directory,
            staged_descriptor,
            staged_identity,
            target_name,
        )

    def _scrub_immutable_derivative_sync(
        self,
        batch_id: str,
        outcome: _DerivativeOutcome,
    ) -> None:
        del batch_id
        failure = False
        try:
            info = os.fstat(outcome.descriptor)
            if (
                self._is_reparse(info)
                or not stat.S_ISREG(info.st_mode)
                or self._identity(info) != outcome.identity
                or info.st_size != outcome.stored.size_bytes
            ):
                failure = True
            scrubbed = False
            for _ in range(2):
                try:
                    os.ftruncate(outcome.descriptor, 0)
                    os.fsync(outcome.descriptor)
                    scrubbed = True
                    break
                except OSError:
                    continue
            if not scrubbed or not self._cleanup_staged_name(
                outcome.directory,
                outcome.name,
                outcome.identity,
                descriptor=outcome.descriptor,
            ):
                failure = True
        finally:
            try:
                os.close(outcome.descriptor)
            except OSError:
                failure = True
            if not self._close_held_directory(outcome.directory):
                failure = True
        if failure:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)

    def _commit_immutable_derivative_sync(
        self, outcome: _DerivativeOutcome
    ) -> None:
        valid = False
        try:
            info = os.fstat(outcome.descriptor)
            valid = (
                not self._is_reparse(info)
                and stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and self._identity(info) == outcome.identity
                and info.st_size == outcome.stored.size_bytes
                and self._directory_unchanged(outcome.directory)
            )
            if valid:
                self._assert_name_identity(
                    outcome.directory, outcome.name, outcome.identity
                )
        except OSError:
            valid = False
        if not valid:
            self._scrub_immutable_derivative_sync("", outcome)
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        # Identity/name/directory validation above is the commit point.  Close
        # failures after it are resource diagnostics only: retrying an
        # ambiguous close could target a reused descriptor, and converting the
        # committed publication to an application failure would orphan an
        # otherwise valid immutable result.  No content or path is retained or
        # emitted here; a future content-free diagnostics port may count it.
        try:
            os.close(outcome.descriptor)
        except OSError:
            pass
        self._close_held_directory(outcome.directory)

    @staticmethod
    def _close_held_directory(directory: _OpenedDirectory) -> bool:
        try:
            if directory.descriptor is not None:
                os.close(directory.descriptor)
            elif directory.windows_handle is not None:
                FileStorage._windows_close_handle(directory.windows_handle)
            return True
        except OSError:
            return False

    def batch_usage(self, batch_id: str) -> BatchUsage:
        directory: _OpenedDirectory | None = None
        failure: FileIntakeFailure | None = None
        file_count = 0
        total_bytes = 0
        try:
            directory = self._open_input_directory(batch_id, create=False)
            if directory is None:
                return BatchUsage(file_count=0, total_bytes=0)
            for name in self._list_names(directory):
                path = Path(name)
                if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                try:
                    self._canonical_uuid(path.stem)
                except FileIntakeFailure:
                    continue
                descriptor = self._open_existing_file(directory, name)
                try:
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    file_count += 1
                    total_bytes += info.st_size
                finally:
                    os.close(descriptor)
            if not self._directory_unchanged(directory):
                raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        except FileIntakeFailure as exc:
            failure = exc
        except OSError:
            failure = FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        finally:
            self._close_directory(directory)

        if failure is not None:
            self._clear_exception_context(failure)
            raise failure
        return BatchUsage(file_count=file_count, total_bytes=total_bytes)

    def _find_duplicate(
        self,
        directory: _OpenedDirectory,
        *,
        sha256: str,
        size_bytes: int,
        metadata: ValidatedFileMetadata,
    ) -> StoredFile | None:
        if not self._directory_unchanged(directory):
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        for name in self._list_names(directory):
            candidate = Path(name)
            if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                candidate_id = self._canonical_uuid(candidate.stem)
            except FileIntakeFailure:
                continue
            descriptor = self._open_existing_file(directory, name)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size != size_bytes:
                    continue
                if self._hash_descriptor(descriptor) != sha256:
                    continue
                self._assert_name_identity(directory, name, self._identity(info))
                if not self._directory_unchanged(directory):
                    raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
                return StoredFile(
                    file_id=candidate_id,
                    path=directory.path / name,
                    sha256=sha256,
                    size_bytes=size_bytes,
                    media_type=metadata.media_type,
                    extension=candidate.suffix.lower(),
                    page_count=metadata.page_count,
                    width=metadata.width,
                    height=metadata.height,
                )
            finally:
                os.close(descriptor)
        if not self._directory_unchanged(directory):
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        return None

    def _open_data_root(self, *, create: bool) -> _OpenedDirectory | None:
        if not os.path.lexists(self._data_root):
            if not create:
                return None
            self._ensure_directory(self._data_root)
        return self._open_directory(self._data_root)

    def _open_input_directory(
        self,
        batch_id: str,
        *,
        create: bool,
    ) -> _OpenedDirectory | None:
        canonical_batch_id = self._canonical_uuid(batch_id)
        root = self._open_data_root(create=create)
        if root is None:
            return None
        batch: _OpenedDirectory | None = None
        try:
            batch = self._open_child_directory(
                root, canonical_batch_id, create=create
            )
            if batch is None:
                return None
            return self._open_child_directory(batch, "input", create=create)
        finally:
            self._close_directory(batch)
            self._close_directory(root)

    def _open_child_directory(
        self,
        parent: _OpenedDirectory,
        name: str,
        *,
        create: bool,
    ) -> _OpenedDirectory | None:
        child_path = parent.path / name
        if parent.descriptor is not None:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(name, flags, dir_fd=parent.descriptor)
            except FileNotFoundError:
                if not create:
                    return None
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent.descriptor)
                except FileExistsError:
                    pass
                descriptor = os.open(name, flags, dir_fd=parent.descriptor)
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode):
                os.close(descriptor)
                raise OSError("child is not a directory")
            return _OpenedDirectory(
                path=child_path,
                descriptor=descriptor,
                windows_handle=None,
                identity=self._identity(info),
            )

        if not self._directory_unchanged(parent):
            raise OSError("parent identity changed")
        if not os.path.lexists(child_path):
            if not create:
                return None
            try:
                child_path.mkdir(mode=0o700)
            except FileExistsError:
                pass
        child = self._open_directory(child_path)
        if not self._directory_unchanged(parent):
            self._close_directory(child)
            raise OSError("parent identity changed")
        return child

    def _open_directory(self, path: Path) -> _OpenedDirectory:
        if os.name == "nt":
            handle = self._windows_open_directory_handle(path)
            try:
                identity = self._windows_handle_identity(handle)
            except BaseException:
                self._windows_close_handle(handle)
                raise
            return _OpenedDirectory(
                path=path,
                descriptor=None,
                windows_handle=handle,
                identity=identity,
            )

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            os.close(descriptor)
            raise OSError("not a directory")
        return _OpenedDirectory(
            path=path,
            descriptor=descriptor,
            windows_handle=None,
            identity=self._identity(info),
        )

    def _open_part(self, directory: _OpenedDirectory, name: str) -> int:
        if directory.descriptor is not None:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            return os.open(name, flags, 0o600, dir_fd=directory.descriptor)
        if not self._directory_unchanged(directory):
            raise OSError("directory identity changed")
        descriptor = self._windows_open_file_descriptor(
            directory.path / name,
            create=True,
            delete_access=True,
        )
        if not self._directory_unchanged(directory):
            self._unlink_name(
                directory, name, descriptor=descriptor, missing_ok=True
            )
            os.close(descriptor)
            raise OSError("directory identity changed")
        return descriptor

    def _open_lock_file(self, directory: _OpenedDirectory, name: str) -> tuple[int, bool]:
        if directory.descriptor is not None:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                return os.open(name, flags, 0o600, dir_fd=directory.descriptor), True
            except FileExistsError:
                flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                return os.open(name, flags, dir_fd=directory.descriptor), False
        if not self._directory_unchanged(directory):
            raise OSError("directory identity changed")
        path = directory.path / name
        try:
            descriptor = self._windows_open_file_descriptor(
                path,
                create=True,
                delete_access=False,
                write_access=True,
            )
            created = True
        except OSError as failure:
            if getattr(failure, "winerror", None) not in {80, 183}:
                raise
            descriptor = self._windows_open_file_descriptor(
                path,
                create=False,
                delete_access=False,
                write_access=True,
            )
            created = False
        if not self._directory_unchanged(directory):
            os.close(descriptor)
            raise OSError("directory identity changed")
        return descriptor, created

    def _open_existing_file(self, directory: _OpenedDirectory, name: str) -> int:
        if directory.descriptor is not None:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            return os.open(name, flags, dir_fd=directory.descriptor)
        if not self._directory_unchanged(directory):
            raise OSError("directory identity changed")
        descriptor = self._windows_open_file_descriptor(
            directory.path / name,
            create=False,
            delete_access=False,
        )
        if not self._directory_unchanged(directory):
            os.close(descriptor)
            raise OSError("directory identity changed")
        return descriptor

    def _list_names(self, directory: _OpenedDirectory) -> list[str]:
        if directory.descriptor is not None:
            return [os.fsdecode(name) for name in os.listdir(directory.descriptor)]
        if not self._directory_unchanged(directory):
            raise OSError("directory identity changed")
        with os.scandir(directory.path) as entries:
            names = [entry.name for entry in entries]
        if not self._directory_unchanged(directory):
            raise OSError("directory identity changed")
        return names

    def _assert_staged_identity(
        self,
        directory: _OpenedDirectory,
        name: str,
        descriptor: int,
        expected_identity: tuple[int, int],
    ) -> None:
        self._assert_name_identity(directory, name, expected_identity)
        if self._identity(os.fstat(descriptor)) != expected_identity:
            raise OSError("staged descriptor identity changed")

    def _assert_published_identity(
        self,
        directory: _OpenedDirectory,
        name: str,
        descriptor: int,
        expected_identity: tuple[int, int],
    ) -> None:
        self._assert_name_identity(directory, name, expected_identity)
        if self._identity(os.fstat(descriptor)) != expected_identity:
            raise OSError("published descriptor identity changed")

    def _assert_name_identity(
        self,
        directory: _OpenedDirectory,
        name: str,
        expected_identity: tuple[int, int],
    ) -> None:
        if directory.descriptor is not None:
            info = os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
        else:
            if not self._directory_unchanged(directory):
                raise OSError("directory identity changed")
            info = os.stat(directory.path / name, follow_symlinks=False)
            if self._is_reparse(info) or not self._directory_unchanged(directory):
                raise OSError("name is reparse or directory changed")
        if self._identity(info) != expected_identity:
            raise OSError("name identity changed")

    def _directory_unchanged(self, directory: _OpenedDirectory) -> bool:
        if directory.windows_handle is not None:
            try:
                current = self._windows_open_directory_handle(directory.path)
            except OSError:
                return False
            try:
                return self._windows_handle_identity(current) == directory.identity
            except OSError:
                return False
            finally:
                self._windows_close_handle(current)
        try:
            current = os.stat(directory.path, follow_symlinks=False)
        except OSError:
            return False
        return (
            not self._is_reparse(current)
            and stat.S_ISDIR(current.st_mode)
            and self._identity(current) == directory.identity
        )

    def _publish_no_replace(
        self,
        directory: _OpenedDirectory,
        source_name: str,
        target_name: str,
        *,
        descriptor: int | None = None,
    ) -> None:
        if directory.windows_handle is not None:
            if descriptor is None:
                raise OSError("staged handle required")
            self._windows_rename_open_file(
                descriptor, directory.windows_handle, target_name
            )
            return
        if sys.platform.startswith("linux") and directory.descriptor is not None:
            if self._linux_rename_noreplace(
                directory.descriptor, source_name, target_name
            ):
                return
        self._hardlink_publish(directory, source_name, target_name)

    @staticmethod
    def _linux_rename_noreplace(
        directory_descriptor: int,
        source_name: str,
        target_name: str,
    ) -> bool:
        renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if renameat2 is None:
            return False
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(
            directory_descriptor,
            os.fsencode(source_name),
            directory_descriptor,
            os.fsencode(target_name),
            1,
        )
        if result == 0:
            return True
        error_number = ctypes.get_errno()
        if error_number in (errno.ENOSYS, errno.EINVAL):
            return False
        if error_number == errno.EEXIST:
            raise FileExistsError(error_number, os.strerror(error_number))
        raise OSError(error_number, os.strerror(error_number))

    def _hardlink_publish(
        self,
        directory: _OpenedDirectory,
        source_name: str,
        target_name: str,
    ) -> None:
        if directory.descriptor is not None:
            os.link(
                source_name,
                target_name,
                src_dir_fd=directory.descriptor,
                dst_dir_fd=directory.descriptor,
                follow_symlinks=False,
            )
        else:
            if not self._directory_unchanged(directory):
                raise OSError("directory identity changed")
            os.link(
                directory.path / source_name,
                directory.path / target_name,
                follow_symlinks=False,
            )
        try:
            self._unlink_name(directory, source_name)
        except OSError:
            try:
                self._unlink_name(directory, source_name)
            except OSError:
                try:
                    self._unlink_name(directory, target_name, missing_ok=True)
                except OSError:
                    pass
                raise

    def _cleanup_staged_name(
        self,
        directory: _OpenedDirectory | None,
        name: str,
        expected_identity: tuple[int, int],
        *,
        descriptor: int,
    ) -> bool:
        if directory is None:
            return False
        if directory.windows_handle is None:
            try:
                info = os.stat(
                    name, dir_fd=directory.descriptor, follow_symlinks=False
                )
            except FileNotFoundError:
                return True
            except OSError:
                return False
            if self._is_reparse(info) or self._identity(info) != expected_identity:
                return False
        for _ in range(2):
            try:
                self._unlink_name(
                    directory,
                    name,
                    descriptor=descriptor if os.name == "nt" else None,
                    missing_ok=True,
                )
                return True
            except OSError:
                continue
        return False

    def _unlink_name(
        self,
        directory: _OpenedDirectory,
        name: str,
        *,
        descriptor: int | None = None,
        missing_ok: bool = False,
    ) -> None:
        if directory.windows_handle is not None and descriptor is not None:
            self._windows_delete_open_file(descriptor)
            return
        try:
            if directory.descriptor is not None:
                os.unlink(name, dir_fd=directory.descriptor)
            else:
                if not self._directory_unchanged(directory):
                    raise OSError("directory identity changed")
                os.unlink(directory.path / name)
        except FileNotFoundError:
            if not missing_ok:
                raise

    @staticmethod
    def _write_all(descriptor: int, chunk: bytes | bytearray | memoryview) -> None:
        remaining = memoryview(chunk)
        while remaining:
            written = os.write(descriptor, remaining)
            if written < 1:
                raise OSError("short write")
            remaining = remaining[written:]

    @staticmethod
    def _duplicate_reader(descriptor: int):
        duplicate = os.dup(descriptor)
        os.lseek(duplicate, 0, os.SEEK_SET)
        return os.fdopen(duplicate, "rb")

    @staticmethod
    def _hash_descriptor(descriptor: int) -> str:
        digest = hashlib.sha256()
        os.lseek(descriptor, 0, os.SEEK_SET)
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()

    def _ensure_directory(self, directory: Path) -> None:
        current = Path(directory.anchor)
        for part in directory.parts[1:]:
            current /= part
            if os.path.lexists(current):
                info = os.lstat(current)
                if self._is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                    raise OSError("unsafe directory component")
                continue
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                info = os.lstat(current)
                if self._is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                    raise OSError("unsafe directory race")

    @staticmethod
    def _canonical_uuid(value: str) -> str:
        try:
            canonical = str(UUID(value))
        except (ValueError, AttributeError, TypeError):
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH) from None
        if canonical != value:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        return canonical

    @staticmethod
    def _identity(info: os.stat_result) -> tuple[int, int]:
        return info.st_dev, info.st_ino

    @staticmethod
    def _is_reparse(info: os.stat_result) -> bool:
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & reparse_flag
        )

    @staticmethod
    def _close_directory(directory: _OpenedDirectory | None) -> None:
        if directory is None:
            return
        try:
            if directory.descriptor is not None:
                os.close(directory.descriptor)
            elif directory.windows_handle is not None:
                FileStorage._windows_close_handle(directory.windows_handle)
        except OSError:
            pass

    @staticmethod
    def _clear_exception_context(failure: FileIntakeFailure) -> None:
        failure.__context__ = None
        failure.__cause__ = None
        failure.__suppress_context__ = True

    @staticmethod
    def _try_batch_lock(descriptor: int) -> bool:
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            return True
        import fcntl

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    @staticmethod
    def _release_batch_lock(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)

    @staticmethod
    def _windows_kernel32():
        return ctypes.WinDLL("kernel32", use_last_error=True)

    @classmethod
    def _windows_open_directory_handle(
        cls,
        path: Path,
        *,
        delete_access: bool = False,
        deny_delete_share: bool = False,
    ) -> int:
        kernel32 = cls._windows_kernel32()
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        access = 0x80 | 0x0001
        if delete_access:
            access |= 0x00010000
        handle = create_file(
            str(path),
            access,
            0x1 | 0x2 | (0 if deny_delete_share else 0x4),
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = wintypes.DWORD()
        get_attributes = kernel32.GetFileInformationByHandleEx

        class _AttributeTagInfo(ctypes.Structure):
            _fields_ = (
                ("FileAttributes", wintypes.DWORD),
                ("ReparseTag", wintypes.DWORD),
            )

        attribute_info = _AttributeTagInfo()
        get_attributes.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        get_attributes.restype = wintypes.BOOL
        if not get_attributes(
            handle,
            9,
            ctypes.byref(attribute_info),
            ctypes.sizeof(attribute_info),
        ):
            cls._windows_close_handle(handle)
            raise ctypes.WinError(ctypes.get_last_error())
        if attribute_info.FileAttributes & 0x400:
            cls._windows_close_handle(handle)
            raise OSError("reparse directory")
        return int(handle)

    @classmethod
    def _windows_handle_path(cls, handle: int) -> str:
        kernel32 = cls._windows_kernel32()
        function = kernel32.GetFinalPathNameByHandleW
        function.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        function.restype = wintypes.DWORD
        size = function(handle, None, 0, 0)
        if not size:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(size + 1)
        written = function(handle, buffer, len(buffer), 0)
        if not written or written >= len(buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        value = buffer.value
        return value[4:] if value.startswith("\\\\?\\") else value

    @classmethod
    def _windows_handle_identity(cls, handle: int) -> tuple[int, int]:
        kernel32 = cls._windows_kernel32()

        class _ByHandleInfo(ctypes.Structure):
            _fields_ = (
                ("FileAttributes", wintypes.DWORD),
                ("CreationTimeLow", wintypes.DWORD),
                ("CreationTimeHigh", wintypes.DWORD),
                ("LastAccessTimeLow", wintypes.DWORD),
                ("LastAccessTimeHigh", wintypes.DWORD),
                ("LastWriteTimeLow", wintypes.DWORD),
                ("LastWriteTimeHigh", wintypes.DWORD),
                ("VolumeSerialNumber", wintypes.DWORD),
                ("FileSizeHigh", wintypes.DWORD),
                ("FileSizeLow", wintypes.DWORD),
                ("NumberOfLinks", wintypes.DWORD),
                ("FileIndexHigh", wintypes.DWORD),
                ("FileIndexLow", wintypes.DWORD),
            )

        info = _ByHandleInfo()
        function = kernel32.GetFileInformationByHandle
        function.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ByHandleInfo))
        function.restype = wintypes.BOOL
        if not function(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        file_index = (info.FileIndexHigh << 32) | info.FileIndexLow
        return info.VolumeSerialNumber, file_index

    @classmethod
    def _windows_open_file_descriptor(
        cls,
        path: Path,
        *,
        create: bool,
        delete_access: bool,
        write_access: bool = False,
    ) -> int:
        import msvcrt

        kernel32 = cls._windows_kernel32()
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        access = 0x80000000
        if create or write_access:
            access |= 0x40000000
        if delete_access:
            access |= 0x00010000
        handle = create_file(
            str(path),
            access,
            0x1 | 0x2 | 0x4,
            None,
            1 if create else 3,
            0x00200000,
            None,
        )
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        flags = os.O_BINARY | (os.O_RDWR if create or write_access else os.O_RDONLY)
        try:
            return msvcrt.open_osfhandle(int(handle), flags)
        except BaseException:
            cls._windows_close_handle(int(handle))
            raise

    @classmethod
    def _windows_rename_open_file(
        cls,
        descriptor: int,
        directory_handle: int,
        target_name: str,
    ) -> None:
        import msvcrt

        if Path(target_name).name != target_name or any(
            separator in target_name for separator in ("/", "\\")
        ):
            raise OSError("target must be a simple relative name")
        encoded_name = target_name.encode("utf-16-le")

        class _RenameInfo(ctypes.Structure):
            _fields_ = (
                ("ReplaceIfExists", wintypes.BOOLEAN),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.DWORD),
                ("FileName", wintypes.WCHAR * 1),
            )

        class _IoStatusBlock(ctypes.Structure):
            _fields_ = (
                ("Status", ctypes.c_void_p),
                ("Information", ctypes.c_size_t),
            )

        name_offset = _RenameInfo.FileName.offset
        buffer = ctypes.create_string_buffer(
            ctypes.sizeof(_RenameInfo) + len(encoded_name)
        )
        info = ctypes.cast(buffer, ctypes.POINTER(_RenameInfo)).contents
        info.ReplaceIfExists = False
        info.RootDirectory = directory_handle
        info.FileNameLength = len(encoded_name)
        ctypes.memmove(
            ctypes.addressof(buffer) + name_offset,
            encoded_name,
            len(encoded_name),
        )
        function = ctypes.WinDLL("ntdll", use_last_error=True).NtSetInformationFile
        function.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_IoStatusBlock),
            wintypes.LPVOID,
            wintypes.ULONG,
            ctypes.c_int,
        )
        function.restype = ctypes.c_long
        handle = msvcrt.get_osfhandle(descriptor)
        io_status = _IoStatusBlock()
        status = function(
            handle,
            ctypes.byref(io_status),
            buffer,
            len(buffer),
            10,
        )
        if status == 0:
            return
        unsigned_status = status & 0xFFFFFFFF
        if unsigned_status == 0xC0000035:
            raise FileExistsError(183, "target exists")
        converter = ctypes.WinDLL("ntdll").RtlNtStatusToDosError
        converter.argtypes = (ctypes.c_long,)
        converter.restype = wintypes.ULONG
        raise ctypes.WinError(converter(status))

    @classmethod
    def _windows_delete_open_file(cls, descriptor: int) -> None:
        import msvcrt

        class _DispositionInfo(ctypes.Structure):
            _fields_ = (("DeleteFile", wintypes.BOOLEAN),)

        info = _DispositionInfo(True)
        function = cls._windows_kernel32().SetFileInformationByHandle
        function.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        function.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(descriptor)
        if not function(handle, 4, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _windows_close_handle(handle: int) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        if not kernel32.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())
