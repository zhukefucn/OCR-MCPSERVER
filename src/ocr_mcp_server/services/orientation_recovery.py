"""Transport-neutral contracts for customer-confirmed orientation recovery.

Angles always mean the clockwise rotation required to make a page upright.  They
are detector output, never caller-controlled input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ..domain.files import StoredFile, SupportedMediaType
from ..domain.orientation import (
    OrientationDecision,
    OrientationErrorCode,
    OrientationEvidence,
    OrientationFailure,
    canonical_pages,
)
from ..domain.secondary_ocr import OrthogonalAngle


def _canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False


@dataclass(frozen=True, slots=True)
class OrientationDetectionRequest:
    batch_id: str
    file_id: str
    page_count: int
    pages: tuple[int, ...]

    def __post_init__(self) -> None:
        if (
            not _canonical_uuid(self.batch_id)
            or not _canonical_uuid(self.file_id)
            or type(self.page_count) is not int
            or self.page_count < 1
        ):
            raise ValueError("invalid orientation detection request")
        object.__setattr__(
            self, "pages", canonical_pages(self.pages, page_count=self.page_count)
        )


class PageOrientationDetector(Protocol):
    async def detect(
        self, request: OrientationDetectionRequest
    ) -> tuple[OrientationEvidence, ...]: ...


class ValidatedPageOrientationDetector:
    """Validate an injected detector without exposing its implementation."""

    def __init__(
        self,
        detector: PageOrientationDetector,
        *,
        credibility_threshold: float,
    ) -> None:
        if (
            isinstance(credibility_threshold, bool)
            or not isinstance(credibility_threshold, (int, float))
            or not 0 <= credibility_threshold <= 1
        ):
            raise ValueError("invalid credibility threshold")
        self._detector = detector
        self._threshold = float(credibility_threshold)

    async def detect(
        self, request: OrientationDetectionRequest
    ) -> tuple[OrientationDecision, ...]:
        if not isinstance(request, OrientationDetectionRequest):
            raise OrientationFailure(OrientationErrorCode.REQUEST_INVALID)
        try:
            evidence = tuple(await self._detector.detect(request))
        except Exception as exc:
            raise OrientationFailure(
                OrientationErrorCode.REQUEST_INVALID, cause=exc
            ) from None
        if (
            any(not isinstance(item, OrientationEvidence) for item in evidence)
            or len(evidence) != len(request.pages)
            or {item.page_number for item in evidence} != set(request.pages)
            or len({item.page_number for item in evidence}) != len(evidence)
        ):
            raise OrientationFailure(OrientationErrorCode.REQUEST_INVALID)
        return tuple(
            OrientationDecision.from_evidence(
                item, credible=item.confidence >= self._threshold
            )
            for item in sorted(evidence, key=lambda item: item.page_number)
        )


@dataclass(frozen=True, slots=True)
class OrientationCorrectionRequest:
    batch_id: str
    file_id: str
    media_type: SupportedMediaType
    extension: str
    page_count: int
    decisions: tuple[OrientationDecision, ...]

    def __post_init__(self) -> None:
        extensions = {
            SupportedMediaType.PDF: {".pdf"},
            SupportedMediaType.PNG: {".png"},
            SupportedMediaType.JPEG: {".jpg", ".jpeg"},
        }
        if (
            not _canonical_uuid(self.batch_id)
            or not _canonical_uuid(self.file_id)
            or not isinstance(self.media_type, SupportedMediaType)
            or self.extension not in extensions[self.media_type]
            or type(self.page_count) is not int
            or self.page_count < 1
        ):
            raise ValueError("invalid orientation correction request")
        decisions = tuple(self.decisions)
        if (
            not decisions
            or any(not isinstance(item, OrientationDecision) for item in decisions)
            or any(
                item.page_number > self.page_count
                or not item.credible
                or item.angle is OrthogonalAngle.DEG_0
                for item in decisions
            )
            or len({item.page_number for item in decisions}) != len(decisions)
            or (
                self.media_type is not SupportedMediaType.PDF
                and tuple(item.page_number for item in decisions) != (1,)
            )
        ):
            raise ValueError("invalid orientation correction decisions")
        object.__setattr__(
            self, "decisions", tuple(sorted(decisions, key=lambda item: item.page_number))
        )


class DocumentOrientationCorrector(Protocol):
    async def correct(self, request: OrientationCorrectionRequest) -> StoredFile: ...
