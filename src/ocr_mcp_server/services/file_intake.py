"""Capacity-aware orchestration for uploaded file streams."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol

from ..domain.constants import DEFAULT_MAX_BATCH_SIZE_BYTES, DEFAULT_MAX_FILES
from ..domain.errors import FileIntakeErrorCode, FileIntakeFailure
from ..domain.files import IncomingFile, StoredFile
from ..domain.models import utc_now
from ..domain.retention import ContentWriteGuard
from .file_storage import FileStorage
from .file_validation import FileValidator
from .remote_fetch import RemoteFileFetcher


class ContentWriteGuards(Protocol):
    async def acquire_content_write(
        self,
        batch_id: str,
        writer_id: str,
        *,
        now: datetime,
        lease_seconds: int,
        allow_missing: bool = False,
    ) -> ContentWriteGuard | None: ...

    async def release_content_write(self, guard: ContentWriteGuard) -> None: ...


def validate_batch_capacity(
    existing_files: int,
    existing_bytes: int,
    incoming_declared_sizes: Sequence[int | None],
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_batch_size_bytes: int = DEFAULT_MAX_BATCH_SIZE_BYTES,
) -> None:
    """Reject invalid, unknown, or over-capacity batch size declarations."""

    if existing_files < 0 or existing_bytes < 0:
        raise FileIntakeFailure(FileIntakeErrorCode.BATCH_CAPACITY_EXCEEDED)
    if existing_files + len(incoming_declared_sizes) > max_files:
        raise FileIntakeFailure(FileIntakeErrorCode.BATCH_CAPACITY_EXCEEDED)
    if any(size is None or size < 0 for size in incoming_declared_sizes):
        raise FileIntakeFailure(FileIntakeErrorCode.BATCH_CAPACITY_EXCEEDED)
    incoming_bytes = sum(size for size in incoming_declared_sizes if size is not None)
    if existing_bytes + incoming_bytes > max_batch_size_bytes:
        raise FileIntakeFailure(FileIntakeErrorCode.BATCH_CAPACITY_EXCEEDED)


class FileIntakeService:
    """Safely validate and persist one upload at a time."""

    def __init__(
        self,
        *,
        storage: FileStorage,
        content_write_guards: ContentWriteGuards,
        validator: FileValidator,
        max_files: int,
        max_file_size_bytes: int,
        max_batch_size_bytes: int,
        now_factory: Callable[[], datetime] = utc_now,
        write_lease_seconds: int = 300,
    ) -> None:
        self._storage = storage
        self._content_write_guards = content_write_guards
        self._validator = validator
        self._max_files = max_files
        self._max_file_size_bytes = max_file_size_bytes
        self._max_batch_size_bytes = max_batch_size_bytes
        self._now_factory = now_factory
        self._write_lease_seconds = write_lease_seconds

    async def ingest_upload(
        self,
        batch_id: str,
        incoming: IncomingFile,
    ) -> StoredFile:
        async with self._storage.batch_lock(batch_id):
            guard = await self._content_write_guards.acquire_content_write(
                batch_id,
                "file-intake",
                now=self._now_factory(),
                lease_seconds=self._write_lease_seconds,
                allow_missing=True,
            )
            try:
                usage = self._storage.batch_usage(batch_id)
                validate_batch_capacity(
                    usage.file_count,
                    usage.total_bytes,
                    [0],
                    max_files=self._max_files,
                    max_batch_size_bytes=self._max_batch_size_bytes,
                )
                remaining_batch_bytes = self._max_batch_size_bytes - usage.total_bytes
                actual_limit = min(self._max_file_size_bytes, remaining_batch_bytes)
                return await self._storage.store(
                    batch_id,
                    incoming,
                    max_file_size_bytes=actual_limit,
                    validator=self._validator,
                )
            finally:
                if guard is not None:
                    await self._content_write_guards.release_content_write(guard)

    async def ingest_remote(
        self,
        batch_id: str,
        url: str,
        fetcher: RemoteFileFetcher,
    ) -> StoredFile:
        """Fetch a remote stream and pass it through the upload intake path."""

        async with fetcher.fetch(url) as incoming:
            return await self.ingest_upload(batch_id, incoming)
