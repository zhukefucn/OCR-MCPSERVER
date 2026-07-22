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
    windows_handle: int | None
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
            descriptor = self._open_lock_file(
                lock_dir, f"{canonical_batch_id}.lock"
            )
            if os.fstat(descriptor).st_size == 0:
                self._write_all(descriptor, b"\0")
                os.fsync(descriptor)
            while not acquired:
                acquired = self._try_batch_lock(descriptor)
                if not acquired:
                    await asyncio.sleep(0.01)
            yield
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

    def _open_lock_file(self, directory: _OpenedDirectory, name: str) -> int:
        if directory.descriptor is not None:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            return os.open(name, flags, 0o600, dir_fd=directory.descriptor)
        if not self._directory_unchanged(directory):
            raise OSError("directory identity changed")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        descriptor = os.open(directory.path / name, flags, 0o600)
        if not self._directory_unchanged(directory):
            os.close(descriptor)
            raise OSError("directory identity changed")
        return descriptor

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
        cls, path: Path, *, delete_access: bool = False
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
            0x1 | 0x2 | 0x4,
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
        if create:
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
        flags = os.O_BINARY | (os.O_RDWR if create else os.O_RDONLY)
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
