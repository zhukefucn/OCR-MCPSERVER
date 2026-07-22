"""Symlink-resistant, server-named storage for validated incoming files."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
import ctypes
from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import stat
import sys
from uuid import UUID, uuid4, uuid5

from ..domain.constants import SUPPORTED_EXTENSIONS
from ..domain.errors import FileIntakeErrorCode, FileIntakeFailure
from ..domain.files import IncomingFile, StoredFile
from .file_validation import FileValidator, ValidatedFileMetadata


@dataclass(frozen=True, slots=True)
class BatchUsage:
    file_count: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class _OpenedDirectory:
    path: Path
    descriptor: int | None
    identity: tuple[int, int]


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
    async def batch_lock(self, batch_id: str):
        """Hold a process-shared exclusive lock for one canonical batch UUID."""

        canonical_batch_id = self._canonical_uuid(batch_id)
        lock_dir = self._data_root / ".locks"
        self._assert_contained(lock_dir)
        self._ensure_directory(lock_dir)
        self._assert_safe_chain(lock_dir)
        lock_path = lock_dir / f"{canonical_batch_id}.lock"
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        open_failure: FileIntakeFailure | None = None
        descriptor: int | None = None
        try:
            if os.path.lexists(lock_path) and self._is_reparse(os.lstat(lock_path)):
                raise OSError("reparse lock file")
            descriptor = os.open(lock_path, flags, 0o600)
            lock_info = os.stat(lock_path, follow_symlinks=False)
            if self._is_reparse(lock_info) or self._identity(
                lock_info
            ) != self._identity(os.fstat(descriptor)):
                raise OSError("lock identity changed")
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
        except OSError:
            open_failure = FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        if open_failure is not None:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise open_failure

        acquired = False
        primary: BaseException | None = None
        try:
            while not acquired:
                acquired = self._try_batch_lock(descriptor)
                if not acquired:
                    await asyncio.sleep(0.01)
            yield
        except OSError:
            primary = FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        except BaseException as exc:
            primary = exc
        cleanup_failed = False
        if acquired:
            try:
                self._release_batch_lock(descriptor)
            except OSError:
                cleanup_failed = True
        try:
            os.close(descriptor)
        except OSError:
            cleanup_failed = True
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
        input_dir = self._input_dir(batch_id)
        self._ensure_directory(input_dir)
        self._assert_safe_chain(input_dir)
        directory = self._open_directory(input_dir)
        part_name = f".{uuid4()}.part"
        part_path = input_dir / part_name
        digest = hashlib.sha256()
        size_bytes = 0
        descriptor: int | None = None
        staged_identity: tuple[int, int] | None = None
        result: StoredFile | None = None
        failure: BaseException | None = None
        try:
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
            if self._id_factory is None:
                file_id = str(uuid5(UUID(batch_id), sha256))
            else:
                file_id = self._canonical_uuid(str(self._id_factory()))
            if not self._directory_unchanged(directory):
                raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
            duplicate = self._find_duplicate(
                input_dir,
                sha256=sha256,
                size_bytes=size_bytes,
                metadata=metadata,
            )
            if duplicate is not None:
                result = duplicate
            else:
                target_name = f"{file_id}{metadata.extension}"
                target = input_dir / target_name
                self._assert_contained(target)
                self._assert_staged_identity(directory, part_name, descriptor)
                if os.name == "nt":
                    os.close(descriptor)
                    descriptor = None
                self._publish_no_replace(directory, part_name, target_name)
                if not self._directory_unchanged(directory):
                    self._unlink_name(directory, target_name, missing_ok=True)
                    raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
                result = StoredFile(
                    file_id=file_id,
                    path=target,
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
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    if failure is None:
                        failure = FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
            if staged_identity is not None:
                self._cleanup_staged_name(directory, part_name, staged_identity)
            if directory.descriptor is not None:
                try:
                    os.close(directory.descriptor)
                except OSError:
                    if failure is None and result is None:
                        failure = FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)

        if failure is not None:
            if isinstance(failure, FileIntakeFailure):
                self._clear_exception_context(failure)
            raise failure
        if result is None:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        return result

    def batch_usage(self, batch_id: str) -> BatchUsage:
        input_dir = self._input_dir(batch_id)
        self._assert_safe_existing_chain(input_dir)
        if not input_dir.exists():
            return BatchUsage(file_count=0, total_bytes=0)
        if not input_dir.is_dir() or input_dir.is_symlink():
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)

        file_count = 0
        total_bytes = 0
        scan_failed = False
        try:
            with os.scandir(input_dir) as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    path = Path(entry.path)
                    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                        continue
                    try:
                        if self._canonical_uuid(path.stem) != path.stem:
                            continue
                    except FileIntakeFailure:
                        continue
                    file_count += 1
                    total_bytes += entry.stat(follow_symlinks=False).st_size
        except OSError:
            scan_failed = True
        if scan_failed:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        return BatchUsage(file_count=file_count, total_bytes=total_bytes)

    def _find_duplicate(
        self,
        input_dir: Path,
        *,
        sha256: str,
        size_bytes: int,
        metadata: ValidatedFileMetadata,
    ) -> StoredFile | None:
        try:
            with os.scandir(input_dir) as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    candidate = Path(entry.path)
                    if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
                        continue
                    try:
                        candidate_id = self._canonical_uuid(candidate.stem)
                    except FileIntakeFailure:
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if info.st_size != size_bytes:
                        continue
                    if self._hash_file(candidate) != sha256:
                        continue
                    return StoredFile(
                        file_id=candidate_id,
                        path=candidate,
                        sha256=sha256,
                        size_bytes=size_bytes,
                        media_type=metadata.media_type,
                        extension=candidate.suffix.lower(),
                        page_count=metadata.page_count,
                        width=metadata.width,
                        height=metadata.height,
                    )
        except FileIntakeFailure:
            raise
        except OSError:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH) from None
        return None

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("not a regular file")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _input_dir(self, batch_id: str) -> Path:
        canonical_batch_id = self._canonical_uuid(batch_id)
        candidate = self._data_root / canonical_batch_id / "input"
        self._assert_contained(candidate)
        return candidate

    @staticmethod
    def _canonical_uuid(value: str) -> str:
        invalid = False
        try:
            canonical = str(UUID(value))
        except (ValueError, AttributeError, TypeError):
            invalid = True
            canonical = ""
        if invalid:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        if canonical != value:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        return canonical

    def _assert_contained(self, candidate: Path) -> None:
        invalid = False
        try:
            common = os.path.commonpath((self._data_root, candidate.absolute()))
        except (OSError, ValueError):
            invalid = True
            common = ""
        if invalid:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        if Path(common) != self._data_root:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)

    def _ensure_directory(self, directory: Path) -> None:
        failure: FileIntakeFailure | None = None
        current = Path(directory.anchor)
        try:
            for part in directory.parts[1:]:
                current /= part
                if os.path.lexists(current):
                    info = os.lstat(current)
                    if self._is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                        raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
                else:
                    current.mkdir(mode=0o700)
        except FileIntakeFailure as exc:
            failure = exc
        except OSError:
            failure = FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        if failure is not None:
            self._clear_exception_context(failure)
            raise failure

    @classmethod
    def _assert_safe_existing_chain(cls, path: Path) -> None:
        failure: FileIntakeFailure | None = None
        current = Path(path.anchor)
        try:
            for part in path.parts[1:]:
                current /= part
                if not os.path.lexists(current):
                    return
                if cls._is_reparse(os.lstat(current)):
                    raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        except FileIntakeFailure as exc:
            failure = exc
        except OSError:
            failure = FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        if failure is not None:
            cls._clear_exception_context(failure)
            raise failure

    def _assert_safe_chain(self, path: Path) -> None:
        self._assert_safe_existing_chain(path)

    def _open_directory(self, path: Path) -> _OpenedDirectory:
        failure: FileIntakeFailure | None = None
        try:
            path_info = os.stat(path, follow_symlinks=False)
            if self._is_reparse(path_info) or not stat.S_ISDIR(path_info.st_mode):
                raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
            descriptor: int | None = None
            if os.name != "nt":
                flags = os.O_RDONLY
                flags |= getattr(os, "O_DIRECTORY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(path, flags)
                descriptor_info = os.fstat(descriptor)
                if self._identity(descriptor_info) != self._identity(path_info):
                    os.close(descriptor)
                    raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
            return _OpenedDirectory(
                path=path,
                descriptor=descriptor,
                identity=self._identity(path_info),
            )
        except FileIntakeFailure as exc:
            failure = exc
        except OSError:
            failure = FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        if failure is not None:
            self._clear_exception_context(failure)
            raise failure

    @staticmethod
    def _open_part(directory: _OpenedDirectory, name: str) -> int:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if directory.descriptor is not None:
            return os.open(name, flags, 0o600, dir_fd=directory.descriptor)
        return os.open(directory.path / name, flags, 0o600)

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

    def _assert_staged_identity(
        self,
        directory: _OpenedDirectory,
        name: str,
        descriptor: int,
    ) -> None:
        failure: FileIntakeFailure | None = None
        try:
            current = self._stat_name(directory, name)
            if self._is_reparse(current):
                raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
            if self._identity(current) != self._identity(os.fstat(descriptor)):
                raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        except FileIntakeFailure as exc:
            failure = exc
        except OSError:
            failure = FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        if failure is not None:
            self._clear_exception_context(failure)
            raise failure

    def _directory_unchanged(self, directory: _OpenedDirectory) -> bool:
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
    ) -> None:
        if os.name == "nt":
            os.rename(directory.path / source_name, directory.path / target_name)
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
        directory: _OpenedDirectory,
        name: str,
        expected_identity: tuple[int, int],
    ) -> None:
        try:
            current = self._stat_name(directory, name)
        except FileNotFoundError:
            return
        except OSError:
            return
        if self._is_reparse(current) or self._identity(current) != expected_identity:
            return
        try:
            self._unlink_name(directory, name, missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _stat_name(directory: _OpenedDirectory, name: str) -> os.stat_result:
        if directory.descriptor is not None:
            return os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
        return os.stat(directory.path / name, follow_symlinks=False)

    @staticmethod
    def _unlink_name(
        directory: _OpenedDirectory,
        name: str,
        *,
        missing_ok: bool = False,
    ) -> None:
        try:
            if directory.descriptor is not None:
                os.unlink(name, dir_fd=directory.descriptor)
            else:
                os.unlink(directory.path / name)
        except FileNotFoundError:
            if not missing_ok:
                raise

    @staticmethod
    def _identity(info: os.stat_result) -> tuple[int, int]:
        return info.st_dev, info.st_ino

    @staticmethod
    def _is_reparse(info: os.stat_result) -> bool:
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        file_attributes = getattr(info, "st_file_attributes", 0)
        return stat.S_ISLNK(info.st_mode) or bool(file_attributes & reparse_flag)

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
