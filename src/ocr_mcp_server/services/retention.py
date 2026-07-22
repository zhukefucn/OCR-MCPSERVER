"""Two-phase retention service and fail-closed owned-root deletion."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import stat
from uuid import UUID, uuid4

from ..domain.errors import FileIntakeFailure, RetentionErrorCode, RetentionFailure
from ..domain.retention import RetentionPhase, RetentionRunResult
from ..infra.retention_repository import RetentionRepository
from .file_storage import FileStorage


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _is_reparse(info: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & flag
    )


class OwnedBatchRootDeleter:
    """Isolate one owned batch root, then scrub bytes through pinned handles.

    Descendant names are intentionally never unlinked or removed.  Empty
    tombstones are content-free recovery metadata and may be retained.
    """

    def delete(
        self, root: Path, batch_id: str, *, tombstone_name: str | None = None
    ) -> None:
        failure: RetentionFailure | None = None
        pinned = None
        try:
            root = Path(root)
            canonical = str(UUID(batch_id))
            tombstone_name = tombstone_name or f".retention-{uuid4()}"
            if (
                canonical != batch_id
                or not root.is_absolute()
                or Path(tombstone_name).name != tombstone_name
                or not tombstone_name.startswith(".retention-")
            ):
                raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
            try:
                root_info = os.lstat(root)
            except FileNotFoundError:
                return
            if _is_reparse(root_info) or not stat.S_ISDIR(root_info.st_mode):
                raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
            root_identity = _identity(root_info)
            target = root / canonical
            tombstone = root / tombstone_name
            target_info = self._lstat_optional(target)
            tombstone_info = self._lstat_optional(tombstone)
            target_exists = target_info is not None
            tombstone_exists = tombstone_info is not None
            if target_exists and tombstone_exists:
                raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
            selected = tombstone if tombstone_exists else target
            if self._lstat_optional(selected) is None:
                self._assert_identity(root, root_identity, directory=True)
                return
            pinned = self._open_directory(selected)
            if selected == target:
                os.rename(target, tombstone)
                try:
                    self._assert_identity(root, root_identity, directory=True)
                    self._assert_identity(tombstone, pinned[2], directory=True)
                except RetentionFailure:
                    self._restore_replacement(tombstone, target)
                    raise
            self._scrub_directory(tombstone, pinned)
            self._assert_identity(root, root_identity, directory=True)
            self._assert_identity(tombstone, pinned[2], directory=True)
            if self._lstat_optional(target) is not None:
                raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
        except RetentionFailure as caught:
            failure = caught
        except OSError:
            failure = RetentionFailure(RetentionErrorCode.CLEANUP_FAILED)
        except (ValueError, TypeError):
            failure = RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
        finally:
            self._close_directory(pinned)
        if failure is not None:
            failure.__context__ = None
            failure.__cause__ = None
            failure.__suppress_context__ = True
            raise failure

    def _scrub_directory(self, path: Path, pinned):
        descriptor, windows_handle, expected = pinned
        self._assert_identity(path, expected, directory=True)
        names = (
            sorted(os.fsdecode(name) for name in os.listdir(descriptor))
            if descriptor is not None
            else self._windows_names(path, expected)
        )
        expected_entries = {}
        for name in names:
            if Path(name).name != name or name in (".", ".."):
                raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
            if descriptor is not None:
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                child_path = path / name
            else:
                self._assert_identity(path, expected, directory=True)
                child_path = path / name
                info = os.lstat(child_path)
            if _is_reparse(info):
                raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
            child_identity = _identity(info)
            if stat.S_ISDIR(info.st_mode):
                child = self._open_directory(
                    child_path, parent_descriptor=descriptor, name=name
                )
                try:
                    if child[2] != child_identity:
                        raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
                    child_entries = self._scrub_directory(child_path, child)
                    expected_entries[name] = (child_identity, True, child_entries)
                finally:
                    self._close_directory(child)
            elif stat.S_ISREG(info.st_mode):
                self._scrub_regular(
                    child_path,
                    child_identity,
                    parent_descriptor=descriptor,
                    name=name,
                )
                expected_entries[name] = (child_identity, False, None)
            else:
                raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
        self._verify_directory(path, pinned, expected_entries)
        return expected_entries

    def _scrub_regular(
        self,
        path: Path,
        expected: tuple[int, int],
        *,
        parent_descriptor: int | None,
        name: str,
    ) -> None:
        if parent_descriptor is not None:
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        else:
            descriptor = FileStorage._windows_open_file_descriptor(
                path,
                create=False,
                delete_access=False,
                write_access=True,
            )
        try:
            self._scrub_open_regular(descriptor, expected, path)
        finally:
            os.close(descriptor)

    def _scrub_open_regular(
        self, descriptor: int, expected: tuple[int, int], path: Path
    ) -> None:
        del path
        info = os.fstat(descriptor)
        if _is_reparse(info) or not stat.S_ISREG(info.st_mode) or _identity(info) != expected:
            raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
        os.ftruncate(descriptor, 0)
        os.fsync(descriptor)

    def _verify_directory(self, path: Path, pinned, expected_entries) -> None:
        descriptor, _, expected = pinned
        self._assert_identity(path, expected, directory=True)
        names = (
            sorted(os.fsdecode(name) for name in os.listdir(descriptor))
            if descriptor is not None
            else self._windows_names(path, expected)
        )
        if names != sorted(expected_entries):
            raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
        for name in names:
            child_path = path / name
            info = (
                os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if descriptor is not None
                else os.lstat(child_path)
            )
            child_identity, is_directory, children = expected_entries[name]
            if _is_reparse(info) or _identity(info) != child_identity:
                raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
            if is_directory:
                if not stat.S_ISDIR(info.st_mode):
                    raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
                child = self._open_directory(
                    child_path, parent_descriptor=descriptor, name=name
                )
                try:
                    if child[2] != child_identity:
                        raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
                    self._verify_directory(child_path, child, children)
                finally:
                    self._close_directory(child)
            elif not stat.S_ISREG(info.st_mode) or info.st_size != 0:
                raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)

    def _open_directory(
        self,
        path: Path,
        *,
        parent_descriptor: int | None = None,
        name: str | None = None,
    ):
        if os.name == "nt":
            handle = FileStorage._windows_open_directory_handle(path)
            return None, handle, FileStorage._windows_handle_identity(handle)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            name if parent_descriptor is not None else path,
            flags,
            dir_fd=parent_descriptor,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            os.close(descriptor)
            raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
        return descriptor, None, _identity(info)

    @staticmethod
    def _windows_names(path: Path, expected: tuple[int, int]) -> list[str]:
        OwnedBatchRootDeleter._assert_identity(path, expected, directory=True)
        with os.scandir(path) as entries:
            names = sorted(entry.name for entry in entries)
        OwnedBatchRootDeleter._assert_identity(path, expected, directory=True)
        return names

    @staticmethod
    def _lstat_optional(path: Path) -> os.stat_result | None:
        try:
            return os.lstat(path)
        except FileNotFoundError:
            return None

    @staticmethod
    def _close_directory(pinned) -> None:
        if pinned is None:
            return
        descriptor, windows_handle, _ = pinned
        try:
            if descriptor is not None:
                os.close(descriptor)
            elif windows_handle is not None:
                FileStorage._windows_close_handle(windows_handle)
        except OSError:
            pass

    @staticmethod
    def _restore_replacement(staged: Path, original: Path) -> None:
        try:
            os.lstat(original)
        except FileNotFoundError:
            try:
                os.rename(staged, original)
            except OSError:
                pass
        except OSError:
            pass

    @staticmethod
    def _assert_identity(
        path: Path, expected: tuple[int, int], *, directory: bool
    ) -> None:
        info = os.lstat(path)
        if (
            _is_reparse(info)
            or _identity(info) != expected
            or (directory and not stat.S_ISDIR(info.st_mode))
            or (not directory and not stat.S_ISREG(info.st_mode))
        ):
            raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)


class RetentionService:
    def __init__(
        self,
        repository: RetentionRepository,
        data_root: Path,
        artifact_root: Path,
        *,
        deleter: OwnedBatchRootDeleter | None = None,
    ) -> None:
        self._repository = repository
        self._data_root = Path(os.path.abspath(data_root))
        self._artifact_root = Path(os.path.abspath(artifact_root))
        self._deleter = deleter or OwnedBatchRootDeleter()
        self._storage = FileStorage(self._data_root)

    async def _delete_content(self, claim, *, now: datetime) -> None:
        try:
            async with self._storage.batch_lock(
                claim.batch_id, allow_retired=True
            ) as batch_lock:
                await self._repository.require_content_write_quiescent(claim, now=now)
                data_tombstone = await self._repository.prepare_tombstone(
                    claim, root_kind="data", now=now
                )
                artifact_tombstone = await self._repository.prepare_tombstone(
                    claim, root_kind="artifact", now=now
                )
                self._deleter.delete(
                    self._data_root,
                    claim.batch_id,
                    tombstone_name=data_tombstone,
                )
                self._deleter.delete(
                    self._artifact_root,
                    claim.batch_id,
                    tombstone_name=artifact_tombstone,
                )
                batch_lock.retire()
                await self._repository.complete_content(claim, now=now)
        except FileIntakeFailure:
            raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP) from None

    async def run_once(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: int,
        limit: int,
    ) -> RetentionRunResult:
        claims = await self._repository.claim_due(
            worker_id, now=now, lease_seconds=lease_seconds, limit=limit
        )
        content_deleted = 0
        metadata_purged = 0
        failed = 0
        for claim in claims:
            try:
                if claim.phase is RetentionPhase.CONTENT:
                    await self._delete_content(claim, now=now)
                    content_deleted += 1
                else:
                    await self._repository.purge_metadata(claim, now=now)
                    metadata_purged += 1
            except RetentionFailure as failure:
                failed += 1
                await self._repository.fail_claim(
                    claim, now=now, error_code=failure.code
                )
        return RetentionRunResult(
            claimed=len(claims),
            content_deleted=content_deleted,
            metadata_purged=metadata_purged,
            failed=failed,
        )

    async def delete_task(
        self,
        batch_id: str,
        *,
        now: datetime,
        worker_id: str,
        lease_seconds: int = 300,
    ) -> bool:
        if not await self._repository.request_early_delete(batch_id, now=now):
            return False
        for _ in range(2):
            claim = await self._repository.claim_batch(
                batch_id,
                worker_id,
                now=now,
                lease_seconds=lease_seconds,
            )
            if claim is None:
                raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None
            try:
                if claim.phase is RetentionPhase.CONTENT:
                    await self._delete_content(claim, now=now)
                else:
                    await self._repository.purge_metadata(claim, now=now)
                    return True
            except RetentionFailure as failure:
                await self._repository.fail_claim(claim, now=now, error_code=failure.code)
                failure.__context__ = None
                failure.__cause__ = None
                failure.__suppress_context__ = True
                raise failure
        raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None
