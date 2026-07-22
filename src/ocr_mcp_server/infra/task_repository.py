"""Atomic SQLite task repository with idempotency and leases."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ..domain.constants import (
    DEFAULT_AUDIT_METADATA_RETENTION_DAYS,
    DEFAULT_INPUT_RETENTION_HOURS,
    DEFAULT_INTERMEDIATE_RETENTION_HOURS,
    DEFAULT_RESULT_RETENTION_HOURS,
)
from ..domain.errors import (
    DomainError,
    InputValidationError,
    LeaseConflictError,
    PersistenceError,
    StateTransitionError,
)
from ..domain.models import (
    BatchStatus,
    FileStatus,
    ProcessingStage,
    new_id,
    utc_now,
)
from ..domain.progress import ProgressCounters, ProgressUnit, map_stage_progress
from ..domain.state_machine import aggregate_batch_status, validate_file_transition
from ..domain.tasks import (
    BatchSnapshot,
    CreateBatchResult,
    FileTaskSnapshot,
    LeaseClaim,
    StageEventSnapshot,
)
from .database import SessionFactory
from .task_models import BatchRecord, FileTaskRecord, RetentionRecord, StageEventRecord

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
        input_retention_hours: int = DEFAULT_INPUT_RETENTION_HOURS,
        intermediate_retention_hours: int = DEFAULT_INTERMEDIATE_RETENTION_HOURS,
        result_retention_hours: int = DEFAULT_RESULT_RETENTION_HOURS,
        audit_metadata_retention_days: int = DEFAULT_AUDIT_METADATA_RETENTION_DAYS,
    ) -> CreateBatchResult:
        ids = tuple(file_ids)
        if (
            not idempotency_key.strip()
            or not ids
            or any(not item.strip() for item in ids)
            or len(set(ids)) != len(ids)
            or isinstance(max_attempts, bool)
            or max_attempts < 1
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in (
                    input_retention_hours,
                    intermediate_retention_hours,
                    result_retention_hours,
                    audit_metadata_retention_days,
                )
            )
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
            processing_files=0,
            queued_files=len(ids),
            current_file_id=None,
            progress=12,
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
                progress=12,
                attempt_count=0,
                max_attempts=max_attempts,
                created_at=now,
                updated_at=now,
                version=1,
            )
            for position, file_id in enumerate(ids)
        ]
        retention = RetentionRecord(
            batch_id=batch.id,
            content_due_at=now + timedelta(hours=max(
                input_retention_hours,
                intermediate_retention_hours,
                result_retention_hours,
            )),
            metadata_due_at=now + timedelta(days=audit_metadata_retention_days),
            content_deleted_at=None,
            early_delete=False,
            claim_phase=None,
            claim_token=None,
            claim_owner=None,
            lease_expires_at=None,
            attempt_count=0,
            last_error_code=None,
            created_at=now,
            updated_at=now,
            version=1,
        )
        try:
            async with self._sessions() as session:
                session.add(batch)
                session.add_all(files)
                session.add(retention)
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

    async def list_batch_files(self, batch_id: str) -> tuple[FileTaskSnapshot, ...]:
        try:
            async with self._sessions() as session:
                records = (
                    await session.scalars(
                        select(FileTaskRecord)
                        .where(FileTaskRecord.batch_id == batch_id)
                        .order_by(FileTaskRecord.position)
                    )
                ).all()
                return tuple(self._file_snapshot(record) for record in records)
        except SQLAlchemyError as exc:
            raise PersistenceError(cause=exc) from None

    async def list_batch_events(self, batch_id: str) -> tuple[StageEventSnapshot, ...]:
        try:
            async with self._sessions() as session:
                rows = (
                    await session.execute(
                        select(StageEventRecord, FileTaskRecord.batch_id)
                        .join(FileTaskRecord, StageEventRecord.file_id == FileTaskRecord.id)
                        .where(FileTaskRecord.batch_id == batch_id)
                        .order_by(StageEventRecord.id)
                    )
                ).all()
                return tuple(
                    self._event_snapshot(record, event_batch_id)
                    for record, event_batch_id in rows
                )
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
                    .order_by(
                        BatchRecord.created_at,
                        BatchRecord.id,
                        FileTaskRecord.position,
                    )
                    .limit(1)
                )
                if record is None:
                    await session.commit()
                    return None
                token = str(uuid4())
                self._add_event(
                    session,
                    record,
                    FileStatus.PROCESSING,
                    ProcessingStage.QUEUED,
                    record.progress,
                    None,
                    now,
                    counters=None,
                )
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
        counters: ProgressCounters | None = None,
        error_code: str | None = None,
        now: datetime,
        _clamp_retry_progress: bool = False,
    ) -> FileTaskSnapshot:
        now = _require_time(now)
        if (
            not isinstance(status, FileStatus)
            or not isinstance(stage, ProcessingStage)
            or isinstance(progress, bool)
            or not isinstance(progress, int)
            or (counters is not None and not isinstance(counters, ProgressCounters))
        ):
            raise InputValidationError()
        _validate_error_code(error_code)
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await session.get(FileTaskRecord, file_id)
                if record is None:
                    raise PersistenceError()
                old_status = FileStatus(record.status)
                old_stage = ProcessingStage(record.stage)
                if (
                    old_status is FileStatus.QUEUED
                    and status is FileStatus.PROCESSING
                ):
                    raise StateTransitionError()
                if old_status is FileStatus.PROCESSING:
                    self._require_lease(record, lease_token, now)
                mapped_progress = progress
                if _clamp_retry_progress and record.attempt_count > 1:
                    progress = max(record.progress, mapped_progress)
                validate_file_transition(
                    old_status,
                    old_stage,
                    record.progress,
                    status,
                    stage,
                    progress,
                )
                self._validate_counter_transition(record, stage, counters)
                if counters is not None:
                    expected_progress = map_stage_progress(stage, counters)
                    if _clamp_retry_progress and record.attempt_count > 1:
                        expected_progress = max(record.progress, expected_progress)
                    if expected_progress != progress:
                        raise StateTransitionError()
                self._add_event(
                    session,
                    record,
                    status,
                    stage,
                    progress,
                    error_code,
                    now,
                    counters=counters,
                )
                record.status = status.value
                record.stage = stage.value
                record.progress = progress
                self._set_counters(record, counters)
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

    async def update_progress(
        self,
        file_id: str,
        lease_token: str,
        *,
        stage: ProcessingStage,
        counters: ProgressCounters | None = None,
        now: datetime,
    ) -> FileTaskSnapshot:
        if not isinstance(stage, ProcessingStage):
            raise InputValidationError()
        progress = map_stage_progress(stage, counters)
        return await self.transition_file(
            file_id,
            lease_token,
            status=FileStatus.PROCESSING,
            stage=stage,
            progress=progress,
            counters=counters,
            now=now,
            _clamp_retry_progress=True,
        )

    async def complete_file(
        self,
        file_id: str,
        lease_token: str,
        *,
        with_warnings: bool,
        now: datetime,
    ) -> FileTaskSnapshot:
        if not isinstance(with_warnings, bool):
            raise InputValidationError()
        status = (
            FileStatus.COMPLETED_WITH_WARNINGS
            if with_warnings
            else FileStatus.COMPLETED
        )
        stage = (
            ProcessingStage.COMPLETED_WITH_WARNINGS
            if with_warnings
            else ProcessingStage.COMPLETED
        )
        return await self.transition_file(
            file_id,
            lease_token,
            status=status,
            stage=stage,
            progress=100,
            now=now,
        )

    async def fail_file(
        self,
        file_id: str,
        lease_token: str,
        *,
        error_code: str,
        now: datetime,
    ) -> FileTaskSnapshot:
        return await self.transition_file(
            file_id,
            lease_token,
            status=FileStatus.FAILED,
            stage=ProcessingStage.FAILED,
            progress=100,
            error_code=error_code,
            now=now,
        )

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
                    session,
                    record,
                    status,
                    stage,
                    progress,
                    error_code,
                    now,
                    counters=None,
                )
                record.status = status.value
                record.stage = stage.value
                record.progress = progress
                self._set_counters(record, None)
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

    async def recover_expired_leases(
        self, *, now: datetime
    ) -> tuple[FileTaskSnapshot, ...]:
        now = _require_time(now)
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                records = (
                    await session.scalars(
                        select(FileTaskRecord)
                        .join(BatchRecord, FileTaskRecord.batch_id == BatchRecord.id)
                        .where(
                            FileTaskRecord.status == FileStatus.PROCESSING.value,
                            FileTaskRecord.lease_expires_at <= now,
                        )
                        .order_by(
                            BatchRecord.created_at,
                            BatchRecord.id,
                            FileTaskRecord.position,
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
                        session,
                        record,
                        status,
                        stage,
                        progress,
                        "lease_expired",
                        now,
                        counters=None,
                    )
                    record.status = status.value
                    record.stage = stage.value
                    record.progress = progress
                    self._set_counters(record, None)
                    record.last_error_code = "lease_expired"
                    self._clear_lease(record)
                    record.updated_at = now
                    record.version += 1
                    batch_ids.add(record.batch_id)
                for batch_id in batch_ids:
                    await self._refresh_batch(session, batch_id, now)
                await session.commit()
                return tuple(self._file_snapshot(record) for record in records)
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
        *,
        counters: ProgressCounters | None,
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
                old_completed_units=record.completed_units,
                new_completed_units=(
                    None if counters is None else counters.completed_units
                ),
                old_total_units=record.total_units,
                new_total_units=None if counters is None else counters.total_units,
                old_progress_unit=record.progress_unit,
                new_progress_unit=None if counters is None else counters.unit.value,
                error_code=error_code,
                version=record.version + 1,
                created_at=now,
            )
        )

    @staticmethod
    def _set_counters(
        record: FileTaskRecord, counters: ProgressCounters | None
    ) -> None:
        record.completed_units = None if counters is None else counters.completed_units
        record.total_units = None if counters is None else counters.total_units
        record.progress_unit = None if counters is None else counters.unit.value

    @staticmethod
    def _validate_counter_transition(
        record: FileTaskRecord,
        stage: ProcessingStage,
        counters: ProgressCounters | None,
    ) -> None:
        old_stage = ProcessingStage(record.stage)
        if counters is None or stage is not old_stage or record.completed_units is None:
            return
        old_unit = (
            None if record.progress_unit is None else ProgressUnit(record.progress_unit)
        )
        if (
            counters.unit is not old_unit
            or counters.completed_units < record.completed_units
            or (
                counters.completed_units == record.completed_units
                and counters.total_units == record.total_units
            )
        ):
            raise StateTransitionError()

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
        batch.processing_files = sum(status is FileStatus.PROCESSING for status in statuses)
        batch.queued_files = sum(status is FileStatus.QUEUED for status in statuses)
        batch.current_file_id = next(
            (
                item.id
                for item in sorted(files, key=lambda candidate: candidate.position)
                if FileStatus(item.status) is FileStatus.PROCESSING
            ),
            None,
        )
        average = sum(item.progress for item in files) // len(files)
        if any(status not in _TERMINAL for status in statuses):
            average = min(99, average)
        batch.progress = max(batch.progress, average)
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
            processing_files=record.processing_files,
            queued_files=record.queued_files,
            current_file_id=record.current_file_id,
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
            completed_units=record.completed_units,
            total_units=record.total_units,
            progress_unit=(
                None
                if record.progress_unit is None
                else ProgressUnit(record.progress_unit)
            ),
        )

    @staticmethod
    def _event_snapshot(
        record: StageEventRecord, batch_id: str
    ) -> StageEventSnapshot:
        return StageEventSnapshot(
            id=record.id,
            file_id=record.file_id,
            batch_id=batch_id,
            old_status=FileStatus(record.old_status),
            new_status=FileStatus(record.new_status),
            old_stage=ProcessingStage(record.old_stage),
            new_stage=ProcessingStage(record.new_stage),
            old_progress=record.old_progress,
            new_progress=record.new_progress,
            old_completed_units=record.old_completed_units,
            new_completed_units=record.new_completed_units,
            old_total_units=record.old_total_units,
            new_total_units=record.new_total_units,
            old_progress_unit=(
                None
                if record.old_progress_unit is None
                else ProgressUnit(record.old_progress_unit)
            ),
            new_progress_unit=(
                None
                if record.new_progress_unit is None
                else ProgressUnit(record.new_progress_unit)
            ),
            error_code=record.error_code,
            version=record.version,
            created_at=_utc(record.created_at),
        )
