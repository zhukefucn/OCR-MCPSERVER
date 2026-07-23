"""Retention workers for durable uploaded staging inputs."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from .retention import OwnedBatchRootDeleter


class StagingUploadRetention:
    def __init__(self, uploads, data_root: Path) -> None:
        self._uploads = uploads
        self._data_root = Path(data_root).absolute()
        self._deleter = OwnedBatchRootDeleter()

    async def run_once(self, *, now: datetime, limit: int) -> int:
        claimed = await self._uploads.claim_expired(now=now, limit=limit)
        deleted = 0
        for upload in claimed:
            try:
                await asyncio.to_thread(
                    self._deleter.delete,
                    self._data_root,
                    upload.storage_batch_id,
                    tombstone_name=f".retention-upload-{upload.file_id}",
                )
                if await self._uploads.delete_retired(upload.file_id):
                    deleted += 1
            except Exception:
                continue
        return deleted


class ProductionRetentionWorker:
    """Periodically run formal batch retention and staging-upload retention."""

    def __init__(
        self,
        *,
        batch_retention,
        staging_retention: StagingUploadRetention,
        now_factory,
        interval_seconds: int,
        lease_seconds: int,
        batch_size: int,
        worker_id: str,
    ) -> None:
        self._batch_retention = batch_retention
        self._staging_retention = staging_retention
        self._now_factory = now_factory
        self._interval = interval_seconds
        self._lease = lease_seconds
        self._batch_size = batch_size
        self._worker_id = worker_id
        self._task: asyncio.Task[None] | None = None
        self._closing = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(), name="ocr-retention-worker"
        )

    async def close(self) -> None:
        if self._task is None:
            return
        self._closing.set()
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _run(self) -> None:
        while not self._closing.is_set():
            now = self._now_factory()
            try:
                await self._batch_retention.run_once(
                    self._worker_id,
                    now=now,
                    lease_seconds=self._lease,
                    limit=self._batch_size,
                )
                await self._staging_retention.run_once(
                    now=now, limit=self._batch_size
                )
            except Exception:
                pass
            try:
                await asyncio.wait_for(
                    self._closing.wait(), timeout=self._interval
                )
            except TimeoutError:
                pass


__all__ = ["ProductionRetentionWorker", "StagingUploadRetention"]
