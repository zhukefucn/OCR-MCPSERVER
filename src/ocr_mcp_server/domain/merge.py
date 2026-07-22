"""Immutable contracts for structured replacement, publication, and rollback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .models import SecondaryOCREngine
from .secondary_ocr import OrthogonalAngle, SecondaryResultKind


class ReplacementDecision(StrEnum):
    REPLACED = "replaced"
    RETAINED = "retained"


class ReplacementReason(StrEnum):
    REPLACED_TABLE = "replaced_table"
    REPLACED_FORMULA = "replaced_formula"
    OTHER_IMAGE = "other_image"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    INVALID_RESULT = "invalid_result"
    INVALID_CONTENT = "invalid_content"
    ALREADY_STRUCTURED = "already_structured"
    STALE_REFERENCE = "stale_or_changed_reference"
    STANDALONE_REFERENCE = "standalone_synthetic_reference"


@dataclass(frozen=True, slots=True)
class ReplacementAuditRecord:
    audit_id: str
    task_id: str
    source_version: int
    output_version: int
    candidate_id: str
    processing_record_id: str
    image_sha256: str
    json_pointer: str | None
    page_index: int | None
    node_index: int | None
    original_node_type: str | None
    kind: SecondaryResultKind
    angle: OrthogonalAngle
    confidence: float
    engine: SecondaryOCREngine
    model_versions: Mapping[str, str]
    decision: ReplacementDecision
    reason: ReplacementReason
    timestamp: datetime
    original_node_snapshot: str | None
    replacement_node_snapshot: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_versions", MappingProxyType(dict(self.model_versions)))


@dataclass(frozen=True, slots=True)
class MergePublicationResult:
    task_id: str
    source_version: int
    output_version: int
    publication_directory: Path
    original_snapshot_path: Path
    manifest_path: Path
    audit_path: Path
    records: tuple[ReplacementAuditRecord, ...]
    replacement_count: int
    retained_count: int
    original_sha256: str
    manifest_sha256: str
    audit_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))


@dataclass(frozen=True, slots=True)
class RollbackPublicationResult:
    task_id: str
    source_version: int
    rolled_back_merge_version: int
    output_version: int
    publication_directory: Path
    manifest_path: Path
    audit_path: Path
    manifest_sha256: str
    audit_sha256: str
    record_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_ids", tuple(self.record_ids))
