"""Immutable task values returned by persistence adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .models import BatchStatus, FileStatus, ProcessingStage


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

    def __post_init__(self) -> None:
        _require_utc(self.created_at)
        _require_utc(self.updated_at)
        _require_utc(self.lease_expires_at)


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
