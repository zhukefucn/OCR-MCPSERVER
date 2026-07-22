"""Content-free retention claims and cleanup results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class RetentionPhase(StrEnum):
    CONTENT = "content"
    METADATA = "metadata"


@dataclass(frozen=True, slots=True)
class RetentionSnapshot:
    batch_id: str
    content_due_at: datetime
    metadata_due_at: datetime
    content_deleted_at: datetime | None
    early_delete: bool
    version: int


@dataclass(frozen=True, slots=True)
class RetentionClaim:
    batch_id: str
    phase: RetentionPhase
    claim_token: str
    lease_expires_at: datetime
    attempt: int


@dataclass(frozen=True, slots=True)
class RetentionRunResult:
    claimed: int
    content_deleted: int
    metadata_purged: int
    failed: int
