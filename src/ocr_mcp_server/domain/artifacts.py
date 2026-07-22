"""Immutable, content-free artifact index and replacement-audit contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .merge import ReplacementDecision, ReplacementReason
from .models import SecondaryOCREngine
from .secondary_ocr import OrthogonalAngle, SecondaryResultKind


@dataclass(frozen=True, slots=True)
class ArtifactBundle:
    artifact_id: str
    batch_id: str
    file_task_id: str
    source_version: int
    result_version: int
    storage_key: str
    media_type: str
    size_bytes: int
    sha256: str
    manifest_sha256: str
    audit_metadata_sha256: str
    audit_record_count: int
    created_at: datetime
    expires_at: datetime
    path: Path
    warning_codes: tuple[str, ...]
    replacement_count: int
    retained_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "warning_codes", tuple(self.warning_codes))


@dataclass(frozen=True, slots=True)
class ArtifactSnapshot:
    artifact_id: str
    batch_id: str
    file_task_id: str
    source_version: int
    result_version: int
    storage_key: str
    media_type: str
    size_bytes: int
    sha256: str
    manifest_sha256: str
    audit_metadata_sha256: str
    audit_record_count: int
    created_at: datetime
    expires_at: datetime
    available: bool
    deleted_at: datetime | None
    version: int


@dataclass(frozen=True, slots=True)
class ReplacementAuditMetadataSnapshot:
    audit_id: str
    artifact_id: str
    record_id: str
    candidate_id: str
    file_task_id: str
    batch_id: str
    source_version: int
    output_version: int
    image_sha256: str
    decision: ReplacementDecision
    reason: ReplacementReason
    kind: SecondaryResultKind
    angle: OrthogonalAngle
    confidence: float
    engine: SecondaryOCREngine
    model_versions: Mapping[str, str]
    timestamp: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_versions", MappingProxyType(dict(self.model_versions)))


def replacement_audit_metadata_sha256(
    batch_id: str,
    file_task_id: str,
    records,
) -> str:
    """Hash exactly the fields permitted in retained audit metadata."""

    document = {
        "batch_id": batch_id,
        "file_task_id": file_task_id,
        "records": [
            {
                "audit_id": record.audit_id,
                "record_id": record.processing_record_id,
                "candidate_id": record.candidate_id,
                "source_version": record.source_version,
                "output_version": record.output_version,
                "image_sha256": record.image_sha256,
                "decision": record.decision.value,
                "reason": record.reason.value,
                "kind": record.kind.value,
                "angle": int(record.angle),
                "confidence": record.confidence,
                "engine": record.engine.value,
                "model_versions": dict(sorted(record.model_versions.items())),
                "timestamp": record.timestamp.isoformat(),
            }
            for record in records
        ],
    }
    encoded = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
