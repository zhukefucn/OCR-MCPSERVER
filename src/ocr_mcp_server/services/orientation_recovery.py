"""Transport-neutral contracts for customer-confirmed orientation recovery.

Angles always mean the clockwise rotation required to make a page upright.  They
are detector output, never caller-controlled input.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, TypeAlias
from uuid import UUID

from ..domain.files import StoredFile, SupportedMediaType
from ..domain.orientation import (
    OrientationDecision,
    OrientationErrorCode,
    OrientationEvidence,
    OrientationFailure,
    RecoveryClaim,
    RecoverySnapshot,
    RecoveryState,
    canonical_pages,
)
from ..domain.models import BatchStatus, utc_now
from ..domain.retention import ContentWriteGuard
from ..domain.secondary_ocr import OrthogonalAngle


ProgressCallback: TypeAlias = Callable[[int, int], Awaitable[None]]


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
    expected_source_sha256: str
    expected_source_size_bytes: int
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
            or not isinstance(self.expected_source_sha256, str)
            or len(self.expected_source_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.expected_source_sha256
            )
            or type(self.expected_source_size_bytes) is not int
            or self.expected_source_size_bytes < 1
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


class OrientationRecoveryState(Protocol):
    async def resolve(self, token: str, *, now: datetime) -> RecoverySnapshot: ...

    async def claim(
        self, token: str, pages: tuple[int, ...], *, now: datetime
    ) -> RecoveryClaim: ...

    async def complete(
        self,
        claim: RecoveryClaim,
        *,
        corrected_input_version: int,
        result_batch_id: str,
        result_version: int,
        now: datetime,
    ) -> RecoverySnapshot: ...

    async def fail(
        self,
        claim: RecoveryClaim,
        *,
        state: RecoveryState,
        error_code: str,
        now: datetime,
    ) -> RecoverySnapshot: ...

    async def list_claimed(
        self, *, now: datetime, limit: int
    ) -> tuple[RecoveryClaim, ...]: ...


class RecoveryStorage(Protocol):
    def batch_lock(
        self,
        batch_id: str,
        *,
        marker_registry: object,
        allow_missing_marker: bool = False,
        allow_retired: bool = False,
    ) -> AbstractAsyncContextManager[object]: ...

    async def resolve_stored(
        self, batch_id: str, file_id: str, *, expected_page_count: int
    ) -> StoredFile: ...


class RecoveryContentWriteGuards(Protocol):
    async def acquire_content_write(
        self,
        batch_id: str,
        writer_id: str,
        *,
        now: datetime,
        lease_seconds: int,
        allow_missing: bool = False,
    ) -> ContentWriteGuard | None: ...

    async def release_content_write(self, guard: ContentWriteGuard) -> None: ...


@dataclass(frozen=True, slots=True)
class FullRecoveryPipelineSubmission:
    batch_id: str
    status: BatchStatus
    result_version: int

    def __post_init__(self) -> None:
        if (
            not _canonical_uuid(self.batch_id)
            or self.status is not BatchStatus.QUEUED
            or type(self.result_version) is not int
            or self.result_version < 1
        ):
            raise ValueError("invalid recovery pipeline submission")


class FullRecoveryPipelineRunner(Protocol):
    async def run(
        self,
        corrected: StoredFile,
        *,
        recovery_id: str,
        source_batch_id: str,
        source_result_version: int,
        corrected_input_version: int,
    ) -> FullRecoveryPipelineSubmission: ...

    async def reconcile(
        self, recovery_id: str
    ) -> FullRecoveryPipelineSubmission | None: ...


@dataclass(frozen=True, slots=True)
class RecoveryReconciliationResult:
    scanned: int
    completed: int
    failed: int
    deferred: int

    def __post_init__(self) -> None:
        values = (self.scanned, self.completed, self.failed, self.deferred)
        if (
            any(type(value) is not int or value < 0 for value in values)
            or self.completed + self.failed + self.deferred != self.scanned
        ):
            raise ValueError("invalid recovery reconciliation result")


@dataclass(frozen=True, slots=True)
class OrientationRecoveryCommand:
    recovery_token: str = field(repr=False)
    pages: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.recovery_token, str)
            or not 8 <= len(self.recovery_token) <= 256
        ):
            raise ValueError("invalid recovery command")
        if self.pages is not None:
            pages = tuple(self.pages)
            if (
                not pages
                or any(type(page) is not int or page < 1 for page in pages)
                or len(set(pages)) != len(pages)
            ):
                raise ValueError("invalid recovery command")
            object.__setattr__(self, "pages", pages)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(recovery_token=<redacted>, pages={self.pages!r})"


@dataclass(frozen=True, slots=True)
class OrientationRecoverySubmission:
    batch_id: str
    status: BatchStatus
    result_version: int

    def __post_init__(self) -> None:
        FullRecoveryPipelineSubmission(
            self.batch_id, self.status, self.result_version
        )


class RecoveryServiceErrorCode(StrEnum):
    TOKEN_INVALID = "token_invalid"
    REQUEST_INVALID = "request_invalid"
    CONFLICT = "conflict"
    UNCERTAIN = "uncertain"
    UNAVAILABLE = "unavailable"
    PROCESSING_FAILED = "processing_failed"


class RecoveryServiceFailure(Exception):
    """Content-free service failure; transport mapping belongs to the bridge."""

    def __init__(self, code: RecoveryServiceErrorCode) -> None:
        self.code = code
        Exception.__init__(self, code.value)


class OrientationRecoveryCoordinator:
    """Run one token-bound recovery without exposing low-level controls."""

    def __init__(
        self,
        *,
        repository: OrientationRecoveryState,
        detector,
        corrector: DocumentOrientationCorrector,
        runner: FullRecoveryPipelineRunner,
        storage: RecoveryStorage,
        marker_registry: object,
        content_write_guards: RecoveryContentWriteGuards,
        now_factory: Callable[[], datetime] = utc_now,
        write_lease_seconds: int = 900,
    ) -> None:
        if type(write_lease_seconds) is not int or write_lease_seconds < 1:
            raise ValueError("invalid recovery write lease")
        self._repository = repository
        self._detector = detector
        self._corrector = corrector
        self._runner = runner
        self._storage = storage
        self._marker_registry = marker_registry
        self._content_write_guards = content_write_guards
        self._now_factory = now_factory
        self._write_lease_seconds = write_lease_seconds

    async def reparse(
        self,
        request: OrientationRecoveryCommand,
        *,
        progress: ProgressCallback | None = None,
    ) -> OrientationRecoverySubmission:
        if not isinstance(request, OrientationRecoveryCommand):
            raise RecoveryServiceFailure(RecoveryServiceErrorCode.REQUEST_INVALID)
        await _safe_progress(progress, 5)
        try:
            resolved = await self._repository.resolve(
                request.recovery_token, now=self._now_factory()
            )
            if not isinstance(resolved, RecoverySnapshot):
                raise RuntimeError("invalid recovery state")
            pages = (
                resolved.suspected_pages
                if request.pages is None
                else canonical_pages(request.pages, page_count=resolved.page_count)
            )
            claim = await self._repository.claim(
                request.recovery_token, pages, now=self._now_factory()
            )
            if (
                not isinstance(claim, RecoveryClaim)
                or (claim.acquired and claim.snapshot.state is not RecoveryState.CLAIMED)
            ):
                raise RuntimeError("invalid recovery claim")
        except OrientationFailure as exc:
            raise _map_orientation_failure(exc) from None
        except (ValueError, TypeError):
            raise RecoveryServiceFailure(
                RecoveryServiceErrorCode.REQUEST_INVALID
            ) from None
        except Exception:
            raise RecoveryServiceFailure(
                RecoveryServiceErrorCode.UNAVAILABLE
            ) from None

        if not claim.acquired:
            return await self._replay(claim.snapshot, progress)

        await _safe_progress(progress, 15)
        try:
            async with self._storage.batch_lock(
                claim.snapshot.batch_id,
                marker_registry=self._marker_registry,
                allow_missing_marker=False,
                allow_retired=False,
            ):
                guard = await self._acquire_guard(claim)
                try:
                    source = await self._storage.resolve_stored(
                        claim.snapshot.batch_id,
                        claim.snapshot.file_id,
                        expected_page_count=claim.snapshot.page_count,
                    )
                    await _safe_progress(progress, 30)
                    decisions = await self._detect(claim)
                    credible = tuple(
                        item
                        for item in decisions
                        if item.credible and item.angle is not OrthogonalAngle.DEG_0
                    )
                    if not credible:
                        await self._terminal_failure(
                            claim,
                            state=RecoveryState.UNCERTAIN,
                            error_code="orientation_uncertain",
                        )
                        await _safe_progress(progress, 100)
                        raise RecoveryServiceFailure(RecoveryServiceErrorCode.UNCERTAIN)
                    await _safe_progress(progress, 50)
                    corrected = await self._correct(claim, source, credible)
                    corrected_input_version = claim.snapshot.source_result_version + 1
                    await _safe_progress(progress, 70)
                    submission = await self._run(
                        claim, corrected, corrected_input_version
                    )
                    if (
                        submission.batch_id == claim.snapshot.batch_id
                        or submission.result_version != corrected_input_version
                    ):
                        await self._terminal_failure(
                            claim,
                            state=RecoveryState.FAILED,
                            error_code="orientation_pipeline_failed",
                        )
                        raise RecoveryServiceFailure(
                            RecoveryServiceErrorCode.UNAVAILABLE
                        )
                    try:
                        await self._repository.complete(
                            claim,
                            corrected_input_version=corrected_input_version,
                            result_batch_id=submission.batch_id,
                            result_version=submission.result_version,
                            now=self._now_factory(),
                        )
                    except Exception:
                        raise RecoveryServiceFailure(
                            RecoveryServiceErrorCode.UNAVAILABLE
                        ) from None
                    await _safe_progress(progress, 100)
                    return OrientationRecoverySubmission(
                        batch_id=submission.batch_id,
                        status=submission.status,
                        result_version=submission.result_version,
                    )
                finally:
                    try:
                        await self._content_write_guards.release_content_write(guard)
                    except Exception:
                        pass
        except RecoveryServiceFailure:
            raise
        except Exception:
            await self._terminal_failure(
                claim,
                state=RecoveryState.FAILED,
                error_code="orientation_content_unavailable",
            )
            raise RecoveryServiceFailure(RecoveryServiceErrorCode.CONFLICT) from None

    async def reconcile_incomplete(
        self, *, limit: int = 100
    ) -> RecoveryReconciliationResult:
        """Reconcile durable runner state after a process restart.

        This path is deliberately read/transition only: it never detects,
        corrects, or starts pipeline work.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise RecoveryServiceFailure(RecoveryServiceErrorCode.REQUEST_INVALID)
        try:
            claims = tuple(
                await self._repository.list_claimed(
                    now=self._now_factory(), limit=limit
                )
            )
            if (
                len(claims) > limit
                or any(not isinstance(claim, RecoveryClaim) for claim in claims)
                or len({claim.claim_id for claim in claims}) != len(claims)
            ):
                raise ValueError
        except Exception:
            raise RecoveryServiceFailure(RecoveryServiceErrorCode.UNAVAILABLE) from None

        completed = failed = deferred = 0
        for claim in claims:
            try:
                submission = await self._runner.reconcile(claim.claim_id)
            except Exception:
                deferred += 1
                continue

            expected_version = claim.snapshot.source_result_version + 1
            valid = (
                isinstance(submission, FullRecoveryPipelineSubmission)
                and submission.batch_id != claim.snapshot.batch_id
                and submission.result_version == expected_version
            )
            if valid:
                try:
                    await self._repository.complete(
                        claim,
                        corrected_input_version=expected_version,
                        result_batch_id=submission.batch_id,
                        result_version=submission.result_version,
                        now=self._now_factory(),
                    )
                    completed += 1
                except Exception:
                    deferred += 1
                continue

            # Only an explicit absence is authoritative.  A malformed or
            # foreign response may be a reconciliation dependency fault and
            # must not terminalize a live claim.
            if submission is not None:
                deferred += 1
                continue

            try:
                await self._repository.fail(
                    claim,
                    state=RecoveryState.FAILED,
                    error_code="orientation_recovery_interrupted",
                    now=self._now_factory(),
                )
                failed += 1
            except Exception:
                deferred += 1

        return RecoveryReconciliationResult(
            scanned=len(claims),
            completed=completed,
            failed=failed,
            deferred=deferred,
        )

    async def _replay(
        self, snapshot: RecoverySnapshot, progress: ProgressCallback | None
    ) -> OrientationRecoverySubmission:
        if snapshot.state is RecoveryState.COMPLETED:
            await _safe_progress(progress, 100)
            return OrientationRecoverySubmission(
                batch_id=snapshot.result_batch_id,
                status=BatchStatus.QUEUED,
                result_version=snapshot.result_version,
            )
        if snapshot.state is RecoveryState.UNCERTAIN:
            raise RecoveryServiceFailure(RecoveryServiceErrorCode.UNCERTAIN)
        if snapshot.state is RecoveryState.FAILED:
            if snapshot.error_code in {
                "orientation_detector_unavailable",
                "orientation_pipeline_failed",
            }:
                raise RecoveryServiceFailure(RecoveryServiceErrorCode.UNAVAILABLE)
            if snapshot.error_code == "orientation_content_unavailable":
                raise RecoveryServiceFailure(RecoveryServiceErrorCode.CONFLICT)
            raise RecoveryServiceFailure(RecoveryServiceErrorCode.PROCESSING_FAILED)
        raise RecoveryServiceFailure(RecoveryServiceErrorCode.CONFLICT)

    async def _acquire_guard(self, claim: RecoveryClaim) -> ContentWriteGuard:
        try:
            guard = await self._content_write_guards.acquire_content_write(
                claim.snapshot.batch_id,
                claim.claim_id,
                now=self._now_factory(),
                lease_seconds=self._write_lease_seconds,
                allow_missing=False,
            )
        except Exception:
            guard = None
        if guard is None:
            await self._terminal_failure(
                claim,
                state=RecoveryState.FAILED,
                error_code="orientation_content_unavailable",
            )
            raise RecoveryServiceFailure(RecoveryServiceErrorCode.CONFLICT)
        return guard

    async def _detect(self, claim: RecoveryClaim) -> tuple[OrientationDecision, ...]:
        try:
            result = tuple(
                await self._detector.detect(
                    OrientationDetectionRequest(
                        batch_id=claim.snapshot.batch_id,
                        file_id=claim.snapshot.file_id,
                        page_count=claim.snapshot.page_count,
                        pages=claim.snapshot.selected_pages,
                    )
                )
            )
            if (
                len(result) != len(claim.snapshot.selected_pages)
                or any(not isinstance(item, OrientationDecision) for item in result)
                or {item.page_number for item in result}
                != set(claim.snapshot.selected_pages)
                or len({item.page_number for item in result}) != len(result)
            ):
                raise ValueError
            return tuple(sorted(result, key=lambda item: item.page_number))
        except Exception:
            await self._terminal_failure(
                claim,
                state=RecoveryState.FAILED,
                error_code="orientation_detector_unavailable",
            )
            raise RecoveryServiceFailure(
                RecoveryServiceErrorCode.UNAVAILABLE
            ) from None

    async def _correct(
        self,
        claim: RecoveryClaim,
        source: StoredFile,
        decisions: tuple[OrientationDecision, ...],
    ) -> StoredFile:
        if (
            not isinstance(source, StoredFile)
            or source.file_id != claim.snapshot.file_id
            or source.page_count != claim.snapshot.page_count
        ):
            await self._terminal_failure(
                claim, state=RecoveryState.FAILED,
                error_code="orientation_content_unavailable",
            )
            raise RecoveryServiceFailure(RecoveryServiceErrorCode.CONFLICT)
        try:
            corrected = await self._corrector.correct(
                OrientationCorrectionRequest(
                    batch_id=claim.snapshot.batch_id,
                    file_id=source.file_id,
                    media_type=source.media_type,
                    extension=source.extension,
                    page_count=source.page_count,
                    expected_source_sha256=source.sha256,
                    expected_source_size_bytes=source.size_bytes,
                    decisions=decisions,
                )
            )
            if (
                not isinstance(corrected, StoredFile)
                or corrected.file_id == source.file_id
                or corrected.page_count != source.page_count
                or corrected.media_type is not source.media_type
                or corrected.extension != source.extension
            ):
                raise ValueError
            trusted = await self._storage.resolve_stored(
                claim.snapshot.batch_id,
                corrected.file_id,
                expected_page_count=source.page_count,
            )
            if (
                not _canonical_uuid(corrected.file_id)
                or not isinstance(trusted, StoredFile)
                or trusted != corrected
                or trusted.path != corrected.path
                or trusted.sha256 != corrected.sha256
                or trusted.size_bytes != corrected.size_bytes
                or trusted.media_type is not corrected.media_type
                or trusted.extension != corrected.extension
                or trusted.page_count != corrected.page_count
            ):
                raise ValueError
            return trusted
        except RecoveryServiceFailure:
            raise
        except Exception:
            await self._terminal_failure(
                claim, state=RecoveryState.FAILED,
                error_code="orientation_correction_failed",
            )
            raise RecoveryServiceFailure(
                RecoveryServiceErrorCode.PROCESSING_FAILED
            ) from None

    async def _run(
        self,
        claim: RecoveryClaim,
        corrected: StoredFile,
        corrected_input_version: int,
    ) -> FullRecoveryPipelineSubmission:
        try:
            result = await self._runner.run(
                corrected,
                recovery_id=claim.claim_id,
                source_batch_id=claim.snapshot.batch_id,
                source_result_version=claim.snapshot.source_result_version,
                corrected_input_version=corrected_input_version,
            )
            if not isinstance(result, FullRecoveryPipelineSubmission):
                raise ValueError
            return result
        except Exception:
            await self._terminal_failure(
                claim, state=RecoveryState.FAILED,
                error_code="orientation_pipeline_failed",
            )
            raise RecoveryServiceFailure(
                RecoveryServiceErrorCode.UNAVAILABLE
            ) from None

    async def _terminal_failure(
        self,
        claim: RecoveryClaim,
        *,
        state: RecoveryState,
        error_code: str,
    ) -> None:
        try:
            await self._repository.fail(
                claim, state=state, error_code=error_code, now=self._now_factory()
            )
        except Exception:
            raise RecoveryServiceFailure(
                RecoveryServiceErrorCode.UNAVAILABLE
            ) from None


async def _safe_progress(
    callback: ProgressCallback | None, value: int, total: int = 100
) -> None:
    if callback is None:
        return
    try:
        await callback(value, total)
    except Exception:
        return


def _map_orientation_failure(exc: OrientationFailure) -> RecoveryServiceFailure:
    if exc.code == OrientationErrorCode.TOKEN_INVALID.value:
        return RecoveryServiceFailure(RecoveryServiceErrorCode.TOKEN_INVALID)
    if exc.code == OrientationErrorCode.REQUEST_INVALID.value:
        return RecoveryServiceFailure(RecoveryServiceErrorCode.REQUEST_INVALID)
    if exc.code in {
        OrientationErrorCode.REQUEST_CONFLICT.value,
        OrientationErrorCode.CLAIM_CONFLICT.value,
    }:
        return RecoveryServiceFailure(RecoveryServiceErrorCode.CONFLICT)
    return RecoveryServiceFailure(RecoveryServiceErrorCode.UNAVAILABLE)
