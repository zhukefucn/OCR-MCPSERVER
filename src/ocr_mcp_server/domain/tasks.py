"""Immutable task values returned by persistence adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .models import BatchStatus, FileStatus, ProcessingStage
from .progress import ProgressCounters, ProgressUnit


def _require_utc(value: datetime | None) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("task timestamps must be timezone-aware")
    if value is not None and value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("task timestamps must use UTC")


@dataclass(frozen=True, slots=True)
class BatchSnapshot:
    id: str
    idempotency_key: str
    status: BatchStatus
    total_files: int
    completed_files: int
    failed_files: int
    cancelled_files: int
    progress: int
    created_at: datetime
    updated_at: datetime
    version: int
    processing_files: int = 0
    queued_files: int = 0
    current_file_id: str | None = None

    def __post_init__(self) -> None:
        _require_utc(self.created_at)
        _require_utc(self.updated_at)


@dataclass(frozen=True, slots=True)
class FileTaskSnapshot:
    id: str
    batch_id: str
    position: int
    status: FileStatus
    stage: ProcessingStage
    progress: int
    attempt_count: int
    max_attempts: int
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    version: int
    completed_units: int | None = None
    total_units: int | None = None
    progress_unit: ProgressUnit | None = None

    def __post_init__(self) -> None:
        _require_utc(self.created_at)
        _require_utc(self.updated_at)
        _require_utc(self.lease_expires_at)

    @property
    def counters(self) -> ProgressCounters | None:
        if self.completed_units is None or self.progress_unit is None:
            return None
        return ProgressCounters(
            self.completed_units,
            self.total_units,
            self.progress_unit,
        )


@dataclass(frozen=True, slots=True)
class StageEventSnapshot:
    id: int
    file_id: str
    batch_id: str
    old_status: FileStatus
    new_status: FileStatus
    old_stage: ProcessingStage
    new_stage: ProcessingStage
    old_progress: int
    new_progress: int
    old_completed_units: int | None
    new_completed_units: int | None
    old_total_units: int | None
    new_total_units: int | None
    old_progress_unit: ProgressUnit | None
    new_progress_unit: ProgressUnit | None
    error_code: str | None
    version: int
    created_at: datetime

    def __post_init__(self) -> None:
        _require_utc(self.created_at)


@dataclass(frozen=True, slots=True)
class LeaseClaim:
    file: FileTaskSnapshot
    lease_token: str
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_utc(self.expires_at)


@dataclass(frozen=True, slots=True)
class CreateBatchResult:
    batch: BatchSnapshot
    files: tuple[FileTaskSnapshot, ...]
    created: bool
