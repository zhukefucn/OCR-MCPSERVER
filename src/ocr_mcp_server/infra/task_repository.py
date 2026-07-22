"""Atomic SQLite task repository with idempotency and leases."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ..domain.errors import (
    DomainError,
    InputValidationError,
    LeaseConflictError,
    PersistenceError,
)
from ..domain.models import (
    BatchStatus,
    FileStatus,
    ProcessingStage,
    new_id,
    utc_now,
)
from ..domain.state_machine import aggregate_batch_status, validate_file_transition
from ..domain.tasks import BatchSnapshot, CreateBatchResult, FileTaskSnapshot, LeaseClaim
from .database import SessionFactory
from .task_models import BatchRecord, FileTaskRecord, StageEventRecord

_ERROR_CODE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
_TERMINAL = frozenset(
    {
        FileStatus.COMPLETED,
        FileStatus.COMPLETED_WITH_WARNINGS,
        FileStatus.FAILED,
        FileStatus.CANCELLED,
    }
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InputValidationError()
    return value.astimezone(UTC)


def _validate_error_code(value: str | None) -> None:
    if value is not None and _ERROR_CODE.fullmatch(value) is None:
        raise InputValidationError()


class TaskRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._sessions = session_factory

    async def create_batch(
        self,
        idempotency_key: str,
        file_ids: Sequence[str],
        *,
        max_attempts: int = 3,
    ) -> CreateBatchResult:
        ids = tuple(file_ids)
        if (
            not idempotency_key.strip()
            or not ids
            or any(not item.strip() for item in ids)
            or len(set(ids)) != len(ids)
            or max_attempts < 1
        ):
            raise InputValidationError()
        existing = await self._get_by_key(idempotency_key)
        if existing is not None:
            return existing
        now = utc_now()
        batch = BatchRecord(
            id=new_id(),
            idempotency_key=idempotency_key,
            status=BatchStatus.QUEUED.value,
            total_files=len(ids),
            completed_files=0,
            failed_files=0,
            cancelled_files=0,
            progress=0,
            created_at=now,
            updated_at=now,
            version=1,
        )
        files = [
            FileTaskRecord(
                id=file_id,
                batch_id=batch.id,
                position=position,
                status=FileStatus.QUEUED.value,
                stage=ProcessingStage.QUEUED.value,
                progress=0,
                attempt_count=0,
                max_attempts=max_attempts,
                created_at=now,
                updated_at=now,
                version=1,
            )
            for position, file_id in enumerate(ids)
        ]
        try:
            async with self._sessions() as session:
                session.add(batch)
                session.add_all(files)
                await session.commit()
            return CreateBatchResult(
                batch=self._batch_snapshot(batch),
                files=tuple(self._file_snapshot(item) for item in files),
                created=True,
            )
        except IntegrityError:
            existing = await self._get_by_key(idempotency_key)
            if existing is not None:
                return existing
            raise PersistenceError() from None
        except SQLAlchemyError as exc:
            raise PersistenceError(cause=exc) from None

    async def _get_by_key(self, key: str) -> CreateBatchResult | None:
        try:
            async with self._sessions() as session:
                batch = await session.scalar(
                    select(BatchRecord).where(BatchRecord.idempotency_key == key)
                )
                if batch is None:
                    return None
                files = (
                    await session.scalars(
                        select(FileTaskRecord)
                        .where(FileTaskRecord.batch_id == batch.id)
                        .order_by(FileTaskRecord.position)
                    )
                ).all()
                return CreateBatchResult(
                    self._batch_snapshot(batch),
                    tuple(self._file_snapshot(item) for item in files),
                    False,
                )
        except SQLAlchemyError as exc:
            raise PersistenceError(cause=exc) from None

    async def get_batch(self, batch_id: str) -> BatchSnapshot | None:
        try:
            async with self._sessions() as session:
                record = await session.get(BatchRecord, batch_id)
                return None if record is None else self._batch_snapshot(record)
        except SQLAlchemyError as exc:
            raise PersistenceError(cause=exc) from None

    async def get_file(self, file_id: str) -> FileTaskSnapshot | None:
        try:
            async with self._sessions() as session:
                record = await session.get(FileTaskRecord, file_id)
                return None if record is None else self._file_snapshot(record)
        except SQLAlchemyError as exc:
            raise PersistenceError(cause=exc) from None

    async def claim_next(
        self, worker_id: str, *, now: datetime, lease_seconds: int
    ) -> LeaseClaim | None:
        now = _require_time(now)
        if not worker_id.strip() or lease_seconds < 1:
            raise InputValidationError()
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await session.scalar(
                    select(FileTaskRecord)
                    .join(BatchRecord, FileTaskRecord.batch_id == BatchRecord.id)
                    .where(FileTaskRecord.status == FileStatus.QUEUED.value)
                    .order_by(BatchRecord.created_at, FileTaskRecord.position)
                    .limit(1)
                )
                if record is None:
                    await session.commit()
                    return None
                token = str(uuid4())
                record.status = FileStatus.PROCESSING.value
                record.attempt_count += 1
                record.lease_owner = worker_id
                record.lease_token = token
                record.lease_expires_at = now + timedelta(seconds=lease_seconds)
                record.updated_at = now
                record.version += 1
                await self._refresh_batch(session, record.batch_id, now)
                await session.commit()
                snapshot = self._file_snapshot(record)
                return LeaseClaim(snapshot, token, snapshot.lease_expires_at)
        except SQLAlchemyError as exc:
            raise PersistenceError(cause=exc) from None

    async def heartbeat(
        self,
        file_id: str,
        lease_token: str,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> FileTaskSnapshot:
        now = _require_time(now)
        if lease_seconds < 1:
            raise InputValidationError()
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await session.get(FileTaskRecord, file_id)
                self._require_lease(record, lease_token, now)
                record.lease_expires_at = now + timedelta(seconds=lease_seconds)
                record.updated_at = now
                record.version += 1
                await session.commit()
                return self._file_snapshot(record)
        except DomainError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(cause=exc) from None

    async def transition_file(
        self,
        file_id: str,
        lease_token: str,
        *,
        status: FileStatus,
        stage: ProcessingStage,
        progress: int,
        error_code: str | None = None,
        now: datetime,
    ) -> FileTaskSnapshot:
        now = _require_time(now)
        _validate_error_code(error_code)
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await session.get(FileTaskRecord, file_id)
                if record is None:
                    raise PersistenceError()
                old_status = FileStatus(record.status)
                old_stage = ProcessingStage(record.stage)
                if old_status is FileStatus.PROCESSING:
                    self._require_lease(record, lease_token, now)
                validate_file_transition(
                    old_status,
                    old_stage,
                    record.progress,
                    status,
                    stage,
                    progress,
                )
                self._add_event(
                    session, record, status, stage, progress, error_code, now
                )
                record.status = status.value
                record.stage = stage.value
                record.progress = progress
                record.last_error_code = error_code
                if status in _TERMINAL:
                    self._clear_lease(record)
                record.updated_at = now
                record.version += 1
                await self._refresh_batch(session, record.batch_id, now)
                await session.commit()
                return self._file_snapshot(record)
        except DomainError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(cause=exc) from None

    async def retry_or_fail(
        self,
        file_id: str,
        lease_token: str,
        *,
        error_code: str,
        now: datetime,
    ) -> FileTaskSnapshot:
        now = _require_time(now)
        _validate_error_code(error_code)
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await session.get(FileTaskRecord, file_id)
                self._require_lease(record, lease_token, now)
                if record.attempt_count < record.max_attempts:
                    status, stage, progress = (
                        FileStatus.QUEUED,
                        ProcessingStage.QUEUED,
                        record.progress,
                    )
                else:
                    status, stage, progress = (
                        FileStatus.FAILED,
                        ProcessingStage.FAILED,
                        100,
                    )
                self._add_event(
                    session, record, status, stage, progress, error_code, now
                )
                record.status = status.value
                record.stage = stage.value
                record.progress = progress
                record.last_error_code = error_code
                self._clear_lease(record)
                record.updated_at = now
                record.version += 1
                await self._refresh_batch(session, record.batch_id, now)
                await session.commit()
                return self._file_snapshot(record)
        except DomainError:
            raise
        except SQLAlchemyError as exc:
            raise PersistenceError(cause=exc) from None

    async def recover_expired_leases(self, *, now: datetime) -> int:
        now = _require_time(now)
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                records = (
                    await session.scalars(
                        select(FileTaskRecord).where(
                            FileTaskRecord.status == FileStatus.PROCESSING.value,
                            FileTaskRecord.lease_expires_at <= now,
                        )
                    )
                ).all()
                batch_ids: set[str] = set()
                for record in records:
                    if record.attempt_count < record.max_attempts:
                        status, stage, progress = (
                            FileStatus.QUEUED,
                            ProcessingStage.QUEUED,
                            record.progress,
                        )
                    else:
                        status, stage, progress = (
                            FileStatus.FAILED,
                            ProcessingStage.FAILED,
                            100,
                        )
                    self._add_event(
                        session, record, status, stage, progress, "lease_expired", now
                    )
                    record.status = status.value
                    record.stage = stage.value
                    record.progress = progress
                    record.last_error_code = "lease_expired"
                    self._clear_lease(record)
                    record.updated_at = now
                    record.version += 1
                    batch_ids.add(record.batch_id)
                for batch_id in batch_ids:
                    await self._refresh_batch(session, batch_id, now)
                await session.commit()
                return len(records)
        except SQLAlchemyError as exc:
            raise PersistenceError(cause=exc) from None

    @staticmethod
    def _require_lease(
        record: FileTaskRecord | None, lease_token: str, now: datetime
    ) -> None:
        if (
            record is None
            or record.status != FileStatus.PROCESSING.value
            or record.lease_token is None
            or record.lease_token != lease_token
            or record.lease_expires_at is None
            or _utc(record.lease_expires_at) <= now
        ):
            raise LeaseConflictError()

    @staticmethod
    def _clear_lease(record: FileTaskRecord) -> None:
        record.lease_owner = None
        record.lease_token = None
        record.lease_expires_at = None

    @staticmethod
    def _add_event(
        session,
        record: FileTaskRecord,
        status: FileStatus,
        stage: ProcessingStage,
        progress: int,
        error_code: str | None,
        now: datetime,
    ) -> None:
        session.add(
            StageEventRecord(
                file_id=record.id,
                old_status=record.status,
                new_status=status.value,
                old_stage=record.stage,
                new_stage=stage.value,
                old_progress=record.progress,
                new_progress=progress,
                error_code=error_code,
                created_at=now,
            )
        )

    async def _refresh_batch(self, session, batch_id: str, now: datetime) -> None:
        batch = await session.get(BatchRecord, batch_id)
        files = (
            await session.scalars(
                select(FileTaskRecord).where(FileTaskRecord.batch_id == batch_id)
            )
        ).all()
        statuses = [FileStatus(item.status) for item in files]
        batch.status = aggregate_batch_status(statuses).value
        batch.completed_files = sum(status in {FileStatus.COMPLETED, FileStatus.COMPLETED_WITH_WARNINGS} for status in statuses)
        batch.failed_files = sum(status is FileStatus.FAILED for status in statuses)
        batch.cancelled_files = sum(status is FileStatus.CANCELLED for status in statuses)
        batch.progress = sum(item.progress for item in files) // len(files)
        batch.updated_at = now
        batch.version += 1

    @staticmethod
    def _batch_snapshot(record: BatchRecord) -> BatchSnapshot:
        return BatchSnapshot(
            id=record.id,
            idempotency_key=record.idempotency_key,
            status=BatchStatus(record.status),
            total_files=record.total_files,
            completed_files=record.completed_files,
            failed_files=record.failed_files,
            cancelled_files=record.cancelled_files,
            progress=record.progress,
            created_at=_utc(record.created_at),
            updated_at=_utc(record.updated_at),
            version=record.version,
        )

    @staticmethod
    def _file_snapshot(record: FileTaskRecord) -> FileTaskSnapshot:
        return FileTaskSnapshot(
            id=record.id,
            batch_id=record.batch_id,
            position=record.position,
            status=FileStatus(record.status),
            stage=ProcessingStage(record.stage),
            progress=record.progress,
            attempt_count=record.attempt_count,
            max_attempts=record.max_attempts,
            lease_owner=record.lease_owner,
            lease_token=record.lease_token,
            lease_expires_at=None if record.lease_expires_at is None else _utc(record.lease_expires_at),
            last_error_code=record.last_error_code,
            created_at=_utc(record.created_at),
            updated_at=_utc(record.updated_at),
            version=record.version,
        )
