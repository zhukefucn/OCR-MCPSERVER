"""Content-free contracts for customer-confirmed page-orientation recovery."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from .errors import DomainError
from .secondary_ocr import OrthogonalAngle


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_CODE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
_EVIDENCE_CODE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class RecoveryState(StrEnum):
    ISSUED = "issued"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    DELETED = "deleted"


class OrientationAssessmentState(StrEnum):
    DETECTING = "detecting"
    READY = "ready"
    NO_SUSPICION = "no_suspicion"
    FAILED = "failed"


class OrientationErrorCode(StrEnum):
    TOKEN_INVALID = "orientation_token_invalid"
    REQUEST_INVALID = "orientation_request_invalid"
    REQUEST_CONFLICT = "orientation_request_conflict"
    CLAIM_CONFLICT = "orientation_claim_conflict"
    PERSISTENCE_FAILED = "orientation_persistence_failed"


_SAFE_MESSAGES = {
    OrientationErrorCode.TOKEN_INVALID: "The orientation recovery token is invalid or expired.",
    OrientationErrorCode.REQUEST_INVALID: "The orientation recovery request is invalid.",
    OrientationErrorCode.REQUEST_CONFLICT: "The orientation recovery request conflicts with an earlier request.",
    OrientationErrorCode.CLAIM_CONFLICT: "The orientation recovery state changed concurrently.",
    OrientationErrorCode.PERSISTENCE_FAILED: "The orientation recovery state could not be persisted.",
}


class OrientationFailure(DomainError):
    """Safe failure that retains no raw token, content, path, or backend cause."""

    def __init__(
        self, code: OrientationErrorCode, *, cause: BaseException | None = None
    ) -> None:
        del cause
        self.code = code.value
        self.safe_message = _SAFE_MESSAGES[code]
        Exception.__init__(self, self.safe_message)


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must include a timezone")
    return value.astimezone(UTC)


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def canonical_pages(values: object, *, page_count: int) -> tuple[int, ...]:
    try:
        pages = tuple(values)  # type: ignore[arg-type]
    except TypeError:
        raise ValueError("pages must be iterable") from None
    if (
        not pages
        or any(not _positive(page) or page > page_count for page in pages)
        or len(set(pages)) != len(pages)
    ):
        raise ValueError("pages must be unique positive in-range integers")
    return tuple(sorted(pages))


@dataclass(frozen=True, slots=True)
class RecoveryTokenBinding:
    file_id: str
    batch_id: str
    source_result_version: int
    page_count: int
    suspected_pages: tuple[int, ...]
    expires_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.file_id, str)
            or _IDENTIFIER.fullmatch(self.file_id) is None
            or not _uuid(self.batch_id)
            or not _positive(self.source_result_version)
            or not _positive(self.page_count)
        ):
            raise ValueError("invalid recovery binding")
        object.__setattr__(
            self,
            "suspected_pages",
            canonical_pages(self.suspected_pages, page_count=self.page_count),
        )
        object.__setattr__(self, "expires_at", _aware_utc(self.expires_at))


@dataclass(frozen=True, slots=True)
class OrientationEvidence:
    page_number: int
    angle: OrthogonalAngle
    confidence: float
    evidence_code: str

    def __post_init__(self) -> None:
        if (
            not _positive(self.page_number)
            or not isinstance(self.angle, OrthogonalAngle)
            or isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
            or not isinstance(self.evidence_code, str)
            or _EVIDENCE_CODE.fullmatch(self.evidence_code) is None
        ):
            raise ValueError("invalid orientation evidence")


@dataclass(frozen=True, slots=True)
class OrientationDecision:
    page_number: int
    angle: OrthogonalAngle
    confidence: float
    evidence_code: str
    credible: bool

    def __post_init__(self) -> None:
        OrientationEvidence(
            self.page_number, self.angle, self.confidence, self.evidence_code
        )
        if not isinstance(self.credible, bool):
            raise ValueError("invalid credibility decision")

    @classmethod
    def from_evidence(
        cls, evidence: OrientationEvidence, *, credible: bool
    ) -> OrientationDecision:
        if not isinstance(evidence, OrientationEvidence):
            raise ValueError("invalid evidence")
        return cls(
            evidence.page_number,
            evidence.angle,
            evidence.confidence,
            evidence.evidence_code,
            credible,
        )


@dataclass(frozen=True, slots=True)
class OrientationAssessmentSnapshot:
    file_id: str
    batch_id: str
    result_version: int
    page_count: int
    state: OrientationAssessmentState
    suspected_pages: tuple[int, ...]
    error_code: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.file_id, str)
            or _IDENTIFIER.fullmatch(self.file_id) is None
            or not _uuid(self.batch_id)
            or not _positive(self.result_version)
            or not _positive(self.page_count)
            or not isinstance(self.state, OrientationAssessmentState)
        ):
            raise ValueError("invalid orientation assessment")
        pages = tuple(self.suspected_pages)
        if pages:
            pages = canonical_pages(pages, page_count=self.page_count)
        if (
            (self.state is OrientationAssessmentState.READY) is not bool(pages)
            or (
                self.state is OrientationAssessmentState.FAILED
                and (
                    not isinstance(self.error_code, str)
                    or _CODE.fullmatch(self.error_code) is None
                )
            )
            or (
                self.state is not OrientationAssessmentState.FAILED
                and self.error_code is not None
            )
        ):
            raise ValueError("invalid orientation assessment state")
        object.__setattr__(self, "suspected_pages", pages)
        object.__setattr__(self, "created_at", _aware_utc(self.created_at))
        object.__setattr__(self, "updated_at", _aware_utc(self.updated_at))

    @property
    def evidence_ready(self) -> bool:
        return self.state is OrientationAssessmentState.READY


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    file_id: str
    batch_id: str
    source_result_version: int
    page_count: int
    suspected_pages: tuple[int, ...]
    selected_pages: tuple[int, ...] | None
    expires_at: datetime
    state: RecoveryState
    request_fingerprint: str | None
    claim_id: str | None
    corrected_input_version: int | None
    corrected_input_file_id: str | None
    corrected_input_sha256: str | None
    corrected_input_size_bytes: int | None
    result_batch_id: str | None
    result_version: int | None
    error_code: str | None
    version: int

    def __post_init__(self) -> None:
        binding = RecoveryTokenBinding(
            self.file_id,
            self.batch_id,
            self.source_result_version,
            self.page_count,
            self.suspected_pages,
            self.expires_at,
        )
        object.__setattr__(self, "suspected_pages", binding.suspected_pages)
        object.__setattr__(self, "expires_at", binding.expires_at)
        if self.selected_pages is not None:
            selected = canonical_pages(self.selected_pages, page_count=self.page_count)
            if not set(selected).issubset(self.suspected_pages):
                raise ValueError("selected pages exceed suspicion binding")
            object.__setattr__(self, "selected_pages", selected)
        if (
            not isinstance(self.state, RecoveryState)
            or not _positive(self.version)
            or (self.request_fingerprint is not None and _DIGEST.fullmatch(self.request_fingerprint) is None)
            or (self.claim_id is not None and _IDENTIFIER.fullmatch(self.claim_id) is None)
            or (self.corrected_input_version is not None and not _positive(self.corrected_input_version))
            or (self.corrected_input_file_id is not None and not _uuid(self.corrected_input_file_id))
            or (self.corrected_input_sha256 is not None and _DIGEST.fullmatch(self.corrected_input_sha256) is None)
            or (self.corrected_input_size_bytes is not None and not _positive(self.corrected_input_size_bytes))
            or (self.result_batch_id is not None and not _uuid(self.result_batch_id))
            or (self.result_version is not None and not _positive(self.result_version))
            or (self.error_code is not None and _CODE.fullmatch(self.error_code) is None)
        ):
            raise ValueError("invalid recovery snapshot")
        claimed_fields = (
            self.selected_pages,
            self.request_fingerprint,
            self.claim_id,
        )
        corrected_fields = (
            self.corrected_input_version,
            self.corrected_input_file_id,
            self.corrected_input_sha256,
            self.corrected_input_size_bytes,
        )
        result_fields = (
            self.result_batch_id,
            self.result_version,
        )
        if self.state is RecoveryState.ISSUED and (
            any(value is not None for value in claimed_fields + corrected_fields + result_fields)
            or self.error_code is not None
        ):
            raise ValueError("issued recovery contains claim state")
        if self.state in {
            RecoveryState.CLAIMED,
            RecoveryState.COMPLETED,
            RecoveryState.FAILED,
            RecoveryState.UNCERTAIN,
        } and any(value is None for value in claimed_fields):
            raise ValueError("claimed recovery lacks claim state")
        if self.state is RecoveryState.CLAIMED and (
            any(value is not None for value in result_fields)
            or self.error_code is not None
            or not (
                all(value is None for value in corrected_fields)
                or all(value is not None for value in corrected_fields)
            )
        ):
            raise ValueError("active claim contains terminal state")
        if self.state is RecoveryState.COMPLETED and (
            any(value is None for value in corrected_fields + result_fields)
            or self.error_code is not None
        ):
            raise ValueError("completed recovery lacks result state")
        if self.state in {RecoveryState.FAILED, RecoveryState.UNCERTAIN} and (
            any(value is not None for value in result_fields)
            or not (
                all(value is None for value in corrected_fields)
                or all(value is not None for value in corrected_fields)
            )
            or self.error_code is None
        ):
            raise ValueError("failed recovery lacks failure state")


@dataclass(frozen=True, slots=True)
class RecoveryTokenIssue:
    token: str = field(repr=False)
    snapshot: RecoverySnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or len(self.token) < 32:
            raise ValueError("invalid recovery token")
        if not isinstance(self.snapshot, RecoverySnapshot):
            raise ValueError("invalid recovery snapshot")
        if self.snapshot.state is not RecoveryState.ISSUED:
            raise ValueError("issued token must reference issued state")


@dataclass(frozen=True, slots=True)
class RecoveryClaim:
    claim_id: str
    request_fingerprint: str
    snapshot: RecoverySnapshot
    acquired: bool

    def __post_init__(self) -> None:
        if (
            _IDENTIFIER.fullmatch(self.claim_id) is None
            or _DIGEST.fullmatch(self.request_fingerprint) is None
            or not isinstance(self.snapshot, RecoverySnapshot)
            or not isinstance(self.acquired, bool)
            or self.snapshot.claim_id != self.claim_id
            or self.snapshot.request_fingerprint != self.request_fingerprint
        ):
            raise ValueError("invalid recovery claim")
