"""Immutable contracts for secondary image recognition."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from hashlib import sha256
import math
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol

from .errors import CandidateCollectionErrorCode, CandidateCollectionFailure
from .models import SecondaryOCREngine


class CandidateSourceKind(StrEnum):
    MINERU_NODE = "mineru_node"
    STANDALONE_INPUT = "standalone_input"


class MinerUImageFormat(StrEnum):
    PNG = "png"
    JPEG = "jpeg"
    JPEG2000 = "jpeg2000"
    WEBP = "webp"
    GIF = "gif"
    BMP = "bmp"
    TIFF = "tiff"


class SecondaryProcessingStatus(StrEnum):
    PENDING = "pending"


class SecondaryResultKind(StrEnum):
    TABLE = "table"
    FORMULA = "formula"
    OTHER = "other"
    UNCERTAIN = "uncertain"


class OrthogonalAngle(IntEnum):
    DEG_0 = 0
    DEG_90 = 90
    DEG_180 = 180
    DEG_270 = 270


class SecondaryContentFormat(StrEnum):
    HTML = "html"
    LATEX = "latex"


class SecondaryResultState(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CandidateReference:
    source_kind: CandidateSourceKind
    page_index: int | None = None
    node_index: int | None = None
    json_pointer: str | None = None
    original_node_type: str | None = None
    bbox: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, CandidateSourceKind):
            raise ValueError("invalid candidate source kind")
        if self.bbox is not None:
            object.__setattr__(self, "bbox", tuple(self.bbox))
        if self.source_kind is CandidateSourceKind.STANDALONE_INPUT:
            if any(
                value is not None
                for value in (
                    self.page_index,
                    self.node_index,
                    self.json_pointer,
                    self.original_node_type,
                    self.bbox,
                )
            ):
                raise ValueError("standalone references cannot identify a MinerU node")
            return
        if (
            isinstance(self.page_index, bool)
            or isinstance(self.node_index, bool)
            or not isinstance(self.page_index, int)
            or not isinstance(self.node_index, int)
            or self.page_index < 0
            or self.node_index < 0
            or self.json_pointer != f"/{self.page_index}/{self.node_index}"
        ):
            raise ValueError("invalid MinerU node reference")
        if not isinstance(self.original_node_type, str) or not self.original_node_type:
            raise ValueError("invalid node type")
        if self.bbox is not None:
            _validate_bbox(self.bbox)

    @classmethod
    def standalone_input(cls) -> CandidateReference:
        return cls(source_kind=CandidateSourceKind.STANDALONE_INPUT)


@dataclass(frozen=True, slots=True)
class ImageCandidate:
    candidate_id: str
    file_task_id: str
    result_version: int
    sha256: str
    size_bytes: int
    image_format: MinerUImageFormat
    width: int
    height: int
    primary_path: Path
    alias_paths: tuple[Path, ...]
    references: tuple[CandidateReference, ...]
    node_type_hints: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "alias_paths", tuple(self.alias_paths))
        object.__setattr__(self, "references", tuple(self.references))
        object.__setattr__(self, "node_type_hints", tuple(self.node_type_hints))
        if (
            not isinstance(self.candidate_id, str)
            or not self.candidate_id
            or not isinstance(self.file_task_id, str)
            or not self.file_task_id
            or isinstance(self.result_version, bool)
            or not isinstance(self.result_version, int)
            or self.result_version < 1
        ):
            raise ValueError("invalid candidate identity")
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.sha256)
        ):
            raise ValueError("invalid SHA-256")
        if (
            not isinstance(self.image_format, MinerUImageFormat)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in (self.size_bytes, self.width, self.height)
            )
        ):
            raise ValueError("invalid image metadata")
        if not isinstance(self.primary_path, Path) or any(
            not isinstance(path, Path) for path in self.alias_paths
        ):
            raise ValueError("invalid candidate paths")
        if not self.alias_paths or self.alias_paths[0] != self.primary_path:
            raise ValueError("primary path must be the first alias")
        if len(set(self.alias_paths)) != len(self.alias_paths):
            raise ValueError("duplicate alias path")
        if not self.references:
            raise ValueError("candidate requires a reference")
        if any(
            not isinstance(reference, CandidateReference)
            for reference in self.references
        ) or any(not isinstance(hint, str) or not hint for hint in self.node_type_hints):
            raise ValueError("invalid candidate references or hints")
        expected_hints = tuple(
            dict.fromkeys(
                reference.original_node_type
                for reference in self.references
                if reference.original_node_type is not None
            )
        )
        if self.node_type_hints != expected_hints:
            raise ValueError("node-type hints must preserve reference order")


@dataclass(frozen=True, slots=True)
class SecondaryOcrResult:
    kind: SecondaryResultKind
    angle: OrthogonalAngle
    content: str | None
    content_format: SecondaryContentFormat | None
    confidence: float
    engine: SecondaryOCREngine
    model_versions: Mapping[str, str]
    state: SecondaryResultState

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, SecondaryResultKind)
            or not isinstance(self.angle, OrthogonalAngle)
            or not isinstance(self.engine, SecondaryOCREngine)
            or not isinstance(self.state, SecondaryResultState)
            or (self.content is not None and not isinstance(self.content, str))
            or (
                self.content_format is not None
                and not isinstance(self.content_format, SecondaryContentFormat)
            )
        ):
            raise ValueError("invalid secondary OCR result contract")
        if (
            not _is_finite_number(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("confidence must be finite and between zero and one")
        if (self.content is None) is not (self.content_format is None):
            raise ValueError("content and content format must be present together")
        if self.content is not None and not self.content:
            raise ValueError("recognized content cannot be empty")
        versions = dict(self.model_versions)
        if any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or not value
            for key, value in versions.items()
        ):
            raise ValueError("model versions must be non-empty string pairs")
        object.__setattr__(self, "model_versions", MappingProxyType(versions))


@dataclass(frozen=True, slots=True)
class SecondaryProcessingRecord:
    record_id: str
    candidate_id: str
    file_task_id: str
    result_version: int
    engine: SecondaryOCREngine
    status: SecondaryProcessingStatus = SecondaryProcessingStatus.PENDING
    result: SecondaryOcrResult | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.record_id, str)
            or not self.record_id
            or not isinstance(self.candidate_id, str)
            or not self.candidate_id
            or not isinstance(self.file_task_id, str)
            or not self.file_task_id
            or not isinstance(self.engine, SecondaryOCREngine)
            or not isinstance(self.status, SecondaryProcessingStatus)
            or (self.result is not None and not isinstance(self.result, SecondaryOcrResult))
        ):
            raise ValueError("invalid processing-record identity")
        if (
            isinstance(self.result_version, bool)
            or not isinstance(self.result_version, int)
            or self.result_version < 1
        ):
            raise ValueError("result version must be positive")
        if self.status is SecondaryProcessingStatus.PENDING and self.result is not None:
            raise ValueError("pending records cannot contain a result")

    @classmethod
    def pending_for(
        cls, candidate: ImageCandidate, *, engine: SecondaryOCREngine
    ) -> SecondaryProcessingRecord:
        digest = sha256(
            (
                "secondary-record\0"
                f"{candidate.file_task_id}\0{candidate.result_version}\0{candidate.sha256}"
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            record_id=f"secondary-{digest}",
            candidate_id=candidate.candidate_id,
            file_task_id=candidate.file_task_id,
            result_version=candidate.result_version,
            engine=engine,
        )


@dataclass(frozen=True, slots=True)
class CandidateCollection:
    file_task_id: str
    result_version: int
    candidates: tuple[ImageCandidate, ...]
    processing_records: tuple[SecondaryProcessingRecord, ...]
    total_reference_count: int = field(init=False)

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "candidates", tuple(self.candidates))
            object.__setattr__(
                self, "processing_records", tuple(self.processing_records)
            )
            candidate_ids = tuple(candidate.candidate_id for candidate in self.candidates)
            record_ids = tuple(record.record_id for record in self.processing_records)
            record_candidate_ids = tuple(
                record.candidate_id for record in self.processing_records
            )
            invalid = (
                not isinstance(self.file_task_id, str)
                or not self.file_task_id
                or isinstance(self.result_version, bool)
                or not isinstance(self.result_version, int)
                or self.result_version < 1
                or len(set(candidate_ids)) != len(candidate_ids)
                or len(set(record_ids)) != len(record_ids)
                or len(set(record_candidate_ids)) != len(record_candidate_ids)
                or any(
                    not isinstance(candidate, ImageCandidate)
                    for candidate in self.candidates
                )
                or any(
                    not isinstance(record, SecondaryProcessingRecord)
                    for record in self.processing_records
                )
                or candidate_ids != record_candidate_ids
                or any(
                    candidate.file_task_id != self.file_task_id
                    or candidate.result_version != self.result_version
                    for candidate in self.candidates
                )
                or any(
                    record.file_task_id != self.file_task_id
                    or record.result_version != self.result_version
                    for record in self.processing_records
                )
            )
            if invalid:
                raise CandidateCollectionFailure(
                    CandidateCollectionErrorCode.INVARIANT_VIOLATION
                )
            object.__setattr__(
                self,
                "total_reference_count",
                sum(len(candidate.references) for candidate in self.candidates),
            )
        except CandidateCollectionFailure:
            raise
        except Exception:
            raise CandidateCollectionFailure(
                CandidateCollectionErrorCode.INVARIANT_VIOLATION
            ) from None


class SecondaryOcrProvider(Protocol):
    engine: SecondaryOCREngine

    async def recognize(self, candidate: ImageCandidate) -> SecondaryOcrResult: ...


def _validate_bbox(values: tuple[float, float, float, float]) -> None:
    if len(values) != 4 or any(not _is_finite_number(value) for value in values):
        raise ValueError("invalid bbox")
    x0, y0, x1, y1 = values
    if not (0 <= x0 <= x1 <= 1000 and 0 <= y0 <= y1 <= 1000):
        raise ValueError("invalid bbox")


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)
