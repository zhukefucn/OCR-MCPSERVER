"""Symlink-resistant, server-named storage for validated incoming files."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from uuid import UUID, uuid4

from ..domain.constants import SUPPORTED_EXTENSIONS
from ..domain.errors import FileIntakeErrorCode, FileIntakeFailure
from ..domain.files import IncomingFile, StoredFile
from .file_validation import FileValidator, ValidatedFileMetadata


@dataclass(frozen=True, slots=True)
class BatchUsage:
    file_count: int
    total_bytes: int


class FileStorage:
    """Persist files beneath ``data_root/<batch UUID>/input`` only."""

    def __init__(
        self,
        data_root: Path,
        *,
        id_factory: Callable[[], object] = uuid4,
    ) -> None:
        self._data_root = Path(os.path.abspath(data_root))
        self._id_factory = id_factory

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

        file_id = self._canonical_uuid(str(self._id_factory()))
        part_path = input_dir / f".{file_id}.{uuid4()}.part"
        self._assert_contained(part_path)
        digest = hashlib.sha256()
        size_bytes = 0
        metadata: ValidatedFileMetadata

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(part_path, flags, 0o600)
        except OSError:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH) from None

        try:
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    async for chunk in incoming.content:
                        if not isinstance(chunk, (bytes, bytearray, memoryview)):
                            raise FileIntakeFailure(
                                FileIntakeErrorCode.INVALID_DOCUMENT
                            )
                        if not chunk:
                            continue
                        size_bytes += len(chunk)
                        if size_bytes > max_file_size_bytes:
                            raise FileIntakeFailure(FileIntakeErrorCode.TOO_LARGE)
                        stream.write(chunk)
                        digest.update(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileIntakeFailure:
                raise
            except Exception:
                raise FileIntakeFailure(
                    FileIntakeErrorCode.INVALID_DOCUMENT
                ) from None

            metadata = validator.validate(
                part_path,
                display_name=incoming.display_name,
                declared_mime=incoming.declared_mime,
            )
            sha256 = digest.hexdigest()
            duplicate = self._find_duplicate(
                input_dir,
                sha256=sha256,
                size_bytes=size_bytes,
                metadata=metadata,
            )
            if duplicate is not None:
                return duplicate

            target = input_dir / f"{file_id}{metadata.extension}"
            self._assert_contained(target)
            try:
                os.link(part_path, target, follow_symlinks=False)
            except (FileExistsError, OSError):
                raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH) from None
            part_path.unlink()
            return StoredFile(
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
        finally:
            try:
                part_path.unlink(missing_ok=True)
            except OSError:
                pass

    def batch_usage(self, batch_id: str) -> BatchUsage:
        input_dir = self._input_dir(batch_id)
        self._assert_safe_existing_chain(input_dir)
        if not input_dir.exists():
            return BatchUsage(file_count=0, total_bytes=0)
        if not input_dir.is_dir() or input_dir.is_symlink():
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)

        file_count = 0
        total_bytes = 0
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
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH) from None
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
        try:
            canonical = str(UUID(value))
        except (ValueError, AttributeError, TypeError):
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH) from None
        if canonical != value:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        return canonical

    def _assert_contained(self, candidate: Path) -> None:
        try:
            common = os.path.commonpath((self._data_root, candidate.absolute()))
        except (OSError, ValueError):
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH) from None
        if Path(common) != self._data_root:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)

    def _ensure_directory(self, directory: Path) -> None:
        current = Path(directory.anchor)
        try:
            for part in directory.parts[1:]:
                current /= part
                if os.path.lexists(current):
                    info = os.lstat(current)
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                        raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
                else:
                    current.mkdir()
        except FileIntakeFailure:
            raise
        except OSError:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH) from None

    @staticmethod
    def _assert_safe_existing_chain(path: Path) -> None:
        current = Path(path.anchor)
        try:
            for part in path.parts[1:]:
                current /= part
                if not os.path.lexists(current):
                    return
                if stat.S_ISLNK(os.lstat(current).st_mode):
                    raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH)
        except FileIntakeFailure:
            raise
        except OSError:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSAFE_PATH) from None

    def _assert_safe_chain(self, path: Path) -> None:
        self._assert_safe_existing_chain(path)
