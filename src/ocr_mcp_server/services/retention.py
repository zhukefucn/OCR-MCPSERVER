"""Two-phase retention service and fail-closed owned-root deletion."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import stat
from uuid import UUID, uuid4

from ..domain.errors import RetentionErrorCode, RetentionFailure
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
    """Delete one direct canonical UUID child without following descendants."""

    def delete(self, root: Path, batch_id: str) -> None:
        failure: RetentionFailure | None = None
        try:
            root = Path(root)
            canonical = str(UUID(batch_id))
            if canonical != batch_id or not root.is_absolute():
                raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
            try:
                root_info = os.lstat(root)
            except FileNotFoundError:
                return
            if _is_reparse(root_info) or not stat.S_ISDIR(root_info.st_mode):
                raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
            root_identity = _identity(root_info)
            target = root / canonical
            try:
                target_info = os.lstat(target)
            except FileNotFoundError:
                self._assert_identity(root, root_identity, directory=True)
                return
            if _is_reparse(target_info) or not stat.S_ISDIR(target_info.st_mode):
                raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
            target_identity = _identity(target_info)
            self._delete_directory_contents(target, target_identity)
            self._assert_identity(root, root_identity, directory=True)
            self._assert_identity(target, target_identity, directory=True)
            staged = root / f".cleanup-{uuid4()}"
            os.rename(target, staged)
            try:
                self._assert_identity(root, root_identity, directory=True)
                self._assert_identity(staged, target_identity, directory=True)
            except RetentionFailure:
                self._restore_replacement(staged, target)
                raise
            self._remove_empty_directory(staged, target_identity)
            self._assert_identity(root, root_identity, directory=True)
        except RetentionFailure as caught:
            failure = caught
        except OSError:
            failure = RetentionFailure(RetentionErrorCode.CLEANUP_FAILED)
        except (ValueError, TypeError):
            failure = RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
        if failure is not None:
            failure.__context__ = None
            failure.__cause__ = None
            failure.__suppress_context__ = True
            raise failure

    def _delete_directory_contents(
        self, directory: Path, expected_identity: tuple[int, int]
    ) -> None:
        self._assert_identity(directory, expected_identity, directory=True)
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries)
        self._assert_identity(directory, expected_identity, directory=True)
        for name in names:
            if Path(name).name != name or name in (".", ".."):
                raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
            child = directory / name
            info = os.lstat(child)
            if _is_reparse(info):
                raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
            child_identity = _identity(info)
            if stat.S_ISDIR(info.st_mode):
                self._delete_directory_contents(child, child_identity)
                self._stage_and_remove(directory, expected_identity, child, child_identity, True)
            elif stat.S_ISREG(info.st_mode):
                self._stage_and_remove(directory, expected_identity, child, child_identity, False)
            else:
                raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
        self._assert_identity(directory, expected_identity, directory=True)

    def _stage_and_remove(
        self,
        parent: Path,
        parent_identity: tuple[int, int],
        child: Path,
        child_identity: tuple[int, int],
        directory: bool,
    ) -> None:
        self._assert_identity(parent, parent_identity, directory=True)
        self._assert_identity(child, child_identity, directory=directory)
        staged = parent / f".delete-{uuid4()}"
        os.rename(child, staged)
        try:
            self._assert_identity(parent, parent_identity, directory=True)
            self._assert_identity(staged, child_identity, directory=directory)
        except RetentionFailure:
            self._restore_replacement(staged, child)
            raise
        if directory:
            self._remove_empty_directory(staged, child_identity)
        else:
            self._remove_regular_file(staged, child_identity)
        self._assert_identity(parent, parent_identity, directory=True)

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

    def _remove_empty_directory(
        self, staged: Path, expected: tuple[int, int]
    ) -> None:
        if os.name != "nt":
            self._assert_identity(staged, expected, directory=True)
            os.rmdir(staged)
            return
        handle: int | None = None
        try:
            handle = FileStorage._windows_open_directory_handle(
                staged, delete_access=True
            )
            if FileStorage._windows_handle_identity(handle) != expected:
                raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
            self._windows_delete_handle(handle)
        finally:
            if handle is not None:
                FileStorage._windows_close_handle(handle)

    def _remove_regular_file(
        self, staged: Path, expected: tuple[int, int]
    ) -> None:
        if os.name != "nt":
            self._assert_identity(staged, expected, directory=False)
            os.unlink(staged)
            return
        descriptor: int | None = None
        try:
            descriptor = FileStorage._windows_open_file_descriptor(
                staged, create=False, delete_access=True
            )
            if _identity(os.fstat(descriptor)) != expected:
                raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
            FileStorage._windows_delete_open_file(descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _windows_delete_handle(handle: int) -> None:
        import ctypes
        from ctypes import wintypes

        class _DispositionInfo(ctypes.Structure):
            _fields_ = (("DeleteFile", wintypes.BOOLEAN),)

        info = _DispositionInfo(True)
        function = FileStorage._windows_kernel32().SetFileInformationByHandle
        function.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        function.restype = wintypes.BOOL
        if not function(handle, 4, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())

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
                    self._deleter.delete(self._data_root, claim.batch_id)
                    self._deleter.delete(self._artifact_root, claim.batch_id)
                    await self._repository.complete_content(claim, now=now)
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
        for expected_phase in (RetentionPhase.CONTENT, RetentionPhase.METADATA):
            claim = await self._repository.claim_batch(
                batch_id,
                worker_id,
                now=now,
                lease_seconds=lease_seconds,
            )
            if claim is None or claim.phase is not expected_phase:
                raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None
            try:
                if claim.phase is RetentionPhase.CONTENT:
                    self._deleter.delete(self._data_root, batch_id)
                    self._deleter.delete(self._artifact_root, batch_id)
                    await self._repository.complete_content(claim, now=now)
                else:
                    await self._repository.purge_metadata(claim, now=now)
            except RetentionFailure as failure:
                await self._repository.fail_claim(claim, now=now, error_code=failure.code)
                failure.__context__ = None
                failure.__cause__ = None
                failure.__suppress_context__ = True
                raise failure
        return True
