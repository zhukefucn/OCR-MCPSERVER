from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from pypdf import PdfWriter

from ocr_mcp_server.api.contracts import OrientationReparseRequest
from ocr_mcp_server.api.document_gateway import OrientationRecoveryGateway
from ocr_mcp_server.api.gateway import (
    GatewayConflict,
    GatewayFailure,
    GatewayOrientationUncertain,
    GatewayUnavailable,
)
from ocr_mcp_server.domain.files import IncomingFile, StoredFile, SupportedMediaType
from ocr_mcp_server.domain.errors import FileIntakeFailure
from ocr_mcp_server.domain.models import BatchStatus
from ocr_mcp_server.domain.orientation import (
    OrientationDecision,
    OrientationErrorCode,
    OrientationFailure,
    RecoveryClaim,
    RecoverySnapshot,
    RecoveryState,
)
from ocr_mcp_server.domain.retention import ContentWriteGuard
from ocr_mcp_server.domain.secondary_ocr import OrthogonalAngle
from ocr_mcp_server.services.orientation_recovery import (
    FullRecoveryPipelineSubmission,
    OrientationRecoveryCommand,
    OrientationRecoveryCoordinator,
    OrientationRecoverySubmission,
    RecoveryServiceFailure,
)
from ocr_mcp_server.services.observability import RecoveryOutcome
from ocr_mcp_server.services.file_storage import BatchLockLease, FileStorage
from ocr_mcp_server.services.file_validation import FileValidator


NOW = datetime(2026, 7, 23, tzinfo=UTC)
BATCH_ID = "11111111-1111-4111-8111-111111111111"
FILE_ID = "22222222-2222-4222-8222-222222222222"
RESULT_BATCH_ID = "33333333-3333-4333-8333-333333333333"
ACCEPTED_FILE_ID = "55555555-5555-4555-8555-555555555555"
CORRECTED_FILE_ID = "44444444-4444-4444-8444-444444444444"


def pipeline_submission(
    batch_id: str = RESULT_BATCH_ID,
    result_version: int = 3,
    *,
    adopted_source_file_id: str = CORRECTED_FILE_ID,
    accepted_input_file_id: str = ACCEPTED_FILE_ID,
    accepted_input_sha256: str = "b" * 64,
    accepted_input_size_bytes: int = 10,
) -> FullRecoveryPipelineSubmission:
    return FullRecoveryPipelineSubmission(
        batch_id,
        BatchStatus.QUEUED,
        result_version,
        adopted_source_file_id,
        accepted_input_file_id,
        accepted_input_sha256,
        accepted_input_size_bytes,
    )


def test_pipeline_submission_requires_content_free_durable_takeover_proof():
    valid = pipeline_submission()
    assert valid.adopted_source_file_id == CORRECTED_FILE_ID
    assert valid.accepted_input_file_id == ACCEPTED_FILE_ID
    assert valid.accepted_input_sha256 == "b" * 64
    assert valid.accepted_input_size_bytes == 10
    for changes in (
        {"accepted_input_file_id": "not-a-uuid"},
        {"accepted_input_sha256": "private document text"},
        {"accepted_input_size_bytes": 0},
    ):
        with pytest.raises(ValueError):
            pipeline_submission(**changes)


def snapshot(state: RecoveryState = RecoveryState.ISSUED) -> RecoverySnapshot:
    claimed = state is not RecoveryState.ISSUED
    terminal = state is RecoveryState.COMPLETED
    failure = state in {RecoveryState.FAILED, RecoveryState.UNCERTAIN}
    return RecoverySnapshot(
        file_id=FILE_ID,
        batch_id=BATCH_ID,
        source_result_version=2,
        page_count=4,
        suspected_pages=(2, 4),
        selected_pages=(2, 4) if claimed else None,
        expires_at=NOW + timedelta(hours=1),
        state=state,
        request_fingerprint="a" * 64 if claimed else None,
        claim_id="claim-123" if claimed else None,
        corrected_input_version=3 if claimed else None,
        corrected_input_file_id=CORRECTED_FILE_ID if claimed else None,
        corrected_input_sha256="b" * 64 if claimed else None,
        corrected_input_size_bytes=10 if claimed else None,
        result_batch_id=RESULT_BATCH_ID if terminal else None,
        result_version=3 if terminal else None,
        error_code=("orientation_uncertain" if state is RecoveryState.UNCERTAIN else "orientation_failed") if failure else None,
        version=2 if claimed else 1,
    )


class Repo:
    def __init__(self, state: RecoveryState = RecoveryState.ISSUED):
        self.current = snapshot(state)
        self.claim_calls = 0
        self.complete_calls = 0
        self.fail_calls: list[tuple[RecoveryState, str]] = []
        self.terminal_observed = False

    async def resolve(self, token, *, now):
        assert token == "or_" + "x" * 32
        return self.current

    async def claim(self, token, pages, *, now):
        self.claim_calls += 1
        if self.current.state is RecoveryState.ISSUED:
            self.current = replace(
                self.current,
                state=RecoveryState.CLAIMED,
                selected_pages=tuple(sorted(pages)),
                request_fingerprint="a" * 64,
                claim_id="claim-123",
                version=2,
            )
            acquired = True
        else:
            acquired = False
        return RecoveryClaim("claim-123", "a" * 64, self.current, acquired)

    async def complete(
        self,
        claim,
        *,
        corrected_input_version,
        result_batch_id,
        result_version,
        adopted_source_file_id,
        accepted_input_file_id,
        accepted_input_sha256,
        accepted_input_size_bytes,
        now,
    ):
        self.complete_calls += 1
        assert accepted_input_file_id == ACCEPTED_FILE_ID
        assert adopted_source_file_id == self.current.corrected_input_file_id
        assert accepted_input_sha256 == "b" * 64
        assert accepted_input_size_bytes == 10
        self.current = replace(
            self.current,
            state=RecoveryState.COMPLETED,
            corrected_input_version=corrected_input_version,
            result_batch_id=result_batch_id,
            result_version=result_version,
            version=self.current.version + 1,
        )
        return self.current

    async def bind_corrected_input(
        self,
        claim,
        *,
        corrected_input_version,
        corrected_file_id,
        corrected_sha256,
        corrected_size_bytes,
        now,
    ):
        self.current = replace(
            self.current,
            corrected_input_version=corrected_input_version,
            corrected_input_file_id=corrected_file_id,
            corrected_input_sha256=corrected_sha256,
            corrected_input_size_bytes=corrected_size_bytes,
            version=self.current.version + 1,
        )
        return self.current

    async def fail(self, claim, *, state, error_code, now):
        self.fail_calls.append((state, error_code))
        self.current = replace(
            self.current,
            state=state,
            error_code=error_code,
            version=self.current.version + 1,
        )
        return self.current

    async def mark_terminal_observed(self, claim_id):
        assert claim_id == "claim-123"
        if self.terminal_observed:
            return False
        self.terminal_observed = True
        return True


class Storage:
    def __init__(self):
        self.events: list[str] = []
        self.corrected: dict[str, StoredFile] = {}

    @asynccontextmanager
    async def batch_lock(self, batch_id, *, marker_registry, allow_missing_marker=False, allow_retired=False):
        assert batch_id == BATCH_ID
        assert marker_registry is MARKERS
        assert not allow_missing_marker and not allow_retired
        self.events.append("lock-enter")
        try:
            yield None
        finally:
            self.events.append("lock-exit")

    async def resolve_stored(self, batch_id, file_id, *, expected_page_count):
        self.events.append("resolve")
        if file_id in self.corrected:
            result = self.corrected[file_id]
            assert (batch_id, expected_page_count) == (BATCH_ID, result.page_count)
            return result
        assert (batch_id, file_id, expected_page_count) == (BATCH_ID, FILE_ID, 4)
        return StoredFile(
            file_id=FILE_ID,
            path=Path("server-derived.pdf"),
            sha256="b" * 64,
            size_bytes=10,
            media_type=SupportedMediaType.PDF,
            extension=".pdf",
            page_count=4,
            width=None,
            height=None,
        )


class Guards:
    def __init__(self, storage):
        self.storage = storage
        self.released = 0

    async def acquire_content_write(self, batch_id, writer_id, *, now, lease_seconds, allow_missing=False):
        assert self.storage.events == ["lock-enter"]
        assert not allow_missing
        self.storage.events.append("guard")
        return ContentWriteGuard(batch_id, writer_id, "guard-token", now + timedelta(seconds=lease_seconds))

    async def release_content_write(self, guard):
        self.released += 1
        self.storage.events.append("release")


class Detector:
    def __init__(self, decisions):
        self.decisions = decisions
        self.calls = 0

    async def detect(self, request):
        self.calls += 1
        assert request.pages == (2, 4)
        STORAGE.events.append("detect")
        return self.decisions


class Corrector:
    def __init__(self):
        self.calls = 0

    async def correct(self, request):
        self.calls += 1
        STORAGE.events.append("correct")
        assert tuple(item.page_number for item in request.decisions) == (2,)
        corrected = replace(
            await STORAGE.resolve_stored(BATCH_ID, FILE_ID, expected_page_count=4),
            file_id=str(uuid4()),
            path=Path("trusted-corrected.pdf"),
        )
        STORAGE.corrected[corrected.file_id] = corrected
        return corrected


class Runner:
    def __init__(self):
        self.calls = 0
        self.reconcile_calls: list[str] = []
        self.reconciled = None

    async def run(self, corrected, *, recovery_id, source_batch_id, source_result_version, corrected_input_version):
        self.calls += 1
        STORAGE.events.append("run")
        assert corrected.file_id != FILE_ID
        assert recovery_id == "claim-123"
        assert (source_batch_id, source_result_version, corrected_input_version) == (BATCH_ID, 2, 3)
        result = pipeline_submission(adopted_source_file_id=corrected.file_id)
        self.reconciled = result
        return result

    async def reconcile(self, recovery_id):
        self.reconcile_calls.append(recovery_id)
        return self.reconciled


MARKERS = object()
STORAGE = Storage()


class _UnusedDelegate:
    pass


def public(service):
    return OrientationRecoveryGateway(_UnusedDelegate(), service)


def coordinator(*, repo=None, decisions=None, detector=None, observability=None):
    global STORAGE
    STORAGE = Storage()
    guards = Guards(STORAGE)
    detector = detector or Detector(decisions or (
        OrientationDecision(2, OrthogonalAngle.DEG_90, .99, "metadata", True),
        OrientationDecision(4, OrthogonalAngle.DEG_0, .99, "metadata", True),
    ))
    corrector = Corrector()
    runner = Runner()
    service = OrientationRecoveryCoordinator(
        repository=repo or Repo(),
        detector=detector,
        corrector=corrector,
        runner=runner,
        storage=STORAGE,
        marker_registry=MARKERS,
        content_write_guards=guards,
        now_factory=lambda: NOW,
        observability=observability,
    )
    return service, guards, detector, corrector, runner


class RecoveryObservability:
    def __init__(self):
        self.outcomes = []

    def observe_recovery(self, outcome):
        self.outcomes.append(outcome)


@pytest.mark.asyncio
async def test_recovery_observes_new_completion_once_but_not_completed_replay():
    repo = Repo()
    observations = RecoveryObservability()
    service, *_ = coordinator(repo=repo, observability=observations)
    command = OrientationRecoveryCommand(recovery_token="or_" + "x" * 32)
    await service.reparse(command)
    await service.reparse(command)
    assert observations.outcomes == [RecoveryOutcome.COMPLETED]


@pytest.mark.asyncio
async def test_missing_durable_marker_is_contained_without_memory_dedupe_claim():
    repo = Repo()

    class MissingMarkerRepository:
        def __getattr__(self, name):
            if name == "mark_terminal_observed":
                raise AttributeError(name)
            return getattr(repo, name)

    observations = RecoveryObservability()
    service, *_ = coordinator(
        repo=MissingMarkerRepository(), observability=observations
    )
    result = await service.reparse(
        OrientationRecoveryCommand(recovery_token="or_" + "x" * 32)
    )
    assert result.batch_id == RESULT_BATCH_ID
    assert observations.outcomes == []
    assert not hasattr(service, "_observed_claims")


@pytest.mark.asyncio
async def test_malformed_durable_marker_result_is_contained_without_observation():
    class MalformedMarkerRepo(Repo):
        async def mark_terminal_observed(self, claim_id):
            return "not-a-boolean"

    observations = RecoveryObservability()
    service, *_ = coordinator(
        repo=MalformedMarkerRepo(), observability=observations
    )
    result = await service.reparse(
        OrientationRecoveryCommand(recovery_token="or_" + "x" * 32)
    )
    assert result.batch_id == RESULT_BATCH_ID
    assert observations.outcomes == []


@pytest.mark.asyncio
async def test_recovery_claims_omitted_suspected_pages_holds_lock_and_guard_through_completion():
    repo = Repo()
    service, guards, detector, corrector, runner = coordinator(repo=repo)
    progress = []
    result = await service.reparse(
        OrientationRecoveryCommand(recovery_token="or_" + "x" * 32),
        progress=lambda value, total: _append(progress, value, total),
    )
    assert (result.batch_id, result.status) == (RESULT_BATCH_ID, BatchStatus.QUEUED)
    assert repo.complete_calls == detector.calls == corrector.calls == runner.calls == 1
    assert guards.released == 1
    assert STORAGE.events == [
        "lock-enter", "guard", "resolve", "detect", "correct", "resolve",
        "resolve", "run", "release", "lock-exit"
    ]
    assert progress == sorted(set(progress)) and progress[-1] == 100


async def _append(items, value, total):
    assert total == 100
    items.append(value)


@pytest.mark.asyncio
async def test_completed_replay_returns_submission_without_lock_detection_correction_or_runner():
    repo = Repo(RecoveryState.COMPLETED)
    service, guards, detector, corrector, runner = coordinator(repo=repo)
    result = await service.reparse(OrientationRecoveryCommand(recovery_token="or_" + "x" * 32))
    assert result.batch_id == RESULT_BATCH_ID
    assert result.status is BatchStatus.QUEUED
    assert (detector.calls, corrector.calls, runner.calls, guards.released) == (0, 0, 0, 0)
    assert STORAGE.events == []


@pytest.mark.asyncio
async def test_uncertain_decisions_are_terminal_stable_and_never_correct_or_run():
    repo = Repo()
    decisions = (
        OrientationDecision(2, OrthogonalAngle.DEG_0, .99, "metadata", True),
        OrientationDecision(4, OrthogonalAngle.DEG_90, .2, "metadata", False),
    )
    service, guards, detector, corrector, runner = coordinator(repo=repo, decisions=decisions)
    with pytest.raises(GatewayOrientationUncertain):
        await public(service).reparse_with_page_orientation(OrientationReparseRequest(recovery_token="or_" + "x" * 32))
    with pytest.raises(GatewayOrientationUncertain):
        await public(service).reparse_with_page_orientation(OrientationReparseRequest(recovery_token="or_" + "x" * 32))
    assert repo.fail_calls == [(RecoveryState.UNCERTAIN, "orientation_uncertain")]
    assert (detector.calls, corrector.calls, runner.calls) == (1, 0, 0)
    assert guards.released == 1


@pytest.mark.asyncio
async def test_existing_claim_conflicts_without_duplicate_work():
    service, _, detector, corrector, runner = coordinator(repo=Repo(RecoveryState.CLAIMED))
    with pytest.raises(GatewayConflict):
        await public(service).reparse_with_page_orientation(OrientationReparseRequest(recovery_token="or_" + "x" * 32))
    assert (detector.calls, corrector.calls, runner.calls) == (0, 0, 0)


@pytest.mark.asyncio
async def test_detector_failure_is_terminal_safe_and_guard_released():
    class BrokenDetector:
        calls = 0
        async def detect(self, request):
            self.calls += 1
            raise RuntimeError("private bank statement text")

    repo = Repo()
    service, guards, detector, corrector, runner = coordinator(repo=repo, detector=BrokenDetector())
    with pytest.raises(GatewayUnavailable) as caught:
        await public(service).reparse_with_page_orientation(OrientationReparseRequest(recovery_token="or_" + "x" * 32))
    assert "private" not in repr(caught.value)
    assert repo.fail_calls == [(RecoveryState.FAILED, "orientation_detector_unavailable")]
    assert guards.released == 1
    assert (corrector.calls, runner.calls) == (0, 0)


@pytest.mark.asyncio
async def test_content_disappearing_after_guard_fails_closed_without_correction_or_submission():
    repo = Repo()
    service, guards, detector, corrector, runner = coordinator(repo=repo)

    async def deleted(*args, **kwargs):
        STORAGE.events.append("deleted-race")
        raise OSError("customer filename must not escape")

    STORAGE.resolve_stored = deleted
    with pytest.raises(GatewayConflict) as caught:
        await public(service).reparse_with_page_orientation(
            OrientationReparseRequest(recovery_token="or_" + "x" * 32)
        )
    assert "customer" not in repr(caught.value)
    assert repo.fail_calls == [(RecoveryState.FAILED, "orientation_content_unavailable")]
    assert guards.released == 1
    assert (detector.calls, corrector.calls, runner.calls) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["completed", "uncertain", "failed"])
async def test_guard_release_failure_never_overrides_persisted_terminal_outcome(terminal):
    repo = Repo()
    service, guards, detector, corrector, runner = coordinator(repo=repo)

    async def diagnostic_only(_guard):
        guards.released += 1
        raise RuntimeError("diagnostic release detail")

    guards.release_content_write = diagnostic_only
    if terminal == "uncertain":
        detector.decisions = (
            OrientationDecision(2, OrthogonalAngle.DEG_0, .99, "metadata", True),
            OrientationDecision(4, OrthogonalAngle.DEG_0, .99, "metadata", True),
        )
        expected = GatewayOrientationUncertain
    elif terminal == "failed":
        async def broken(*args, **kwargs):
            raise RuntimeError("processing detail")
        corrector.correct = broken
        expected = GatewayFailure
    else:
        expected = None

    call = public(service).reparse_with_page_orientation(
        OrientationReparseRequest(recovery_token="or_" + "x" * 32)
    )
    if expected is None:
        result = await call
        assert result.batch_id == RESULT_BATCH_ID
        assert repo.current.state is RecoveryState.COMPLETED
    else:
        with pytest.raises(expected):
            await call
    assert guards.released == 1


@pytest.mark.asyncio
async def test_release_failure_during_cancellation_does_not_replace_cancelled_error():
    import asyncio

    entered = asyncio.Event()

    class BlockingDetector:
        async def detect(self, request):
            entered.set()
            await asyncio.Event().wait()

    service, guards, *_ = coordinator(detector=BlockingDetector())

    async def diagnostic_only(_guard):
        raise RuntimeError("release detail")

    guards.release_content_write = diagnostic_only
    task = asyncio.create_task(
        service.reparse(OrientationRecoveryCommand("or_" + "x" * 32))
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_explicit_page_subset_is_the_only_page_detected_and_corrected():
    repo = Repo()
    service, guards, detector, corrector, runner = coordinator(repo=repo)
    decision = OrientationDecision(2, OrthogonalAngle.DEG_180, .99, "metadata", True)

    class OnePageDetector:
        calls = 0
        async def detect(self, request):
            self.calls += 1
            assert request.pages == (2,)
            return (decision,)

    class OnePageCorrector(Corrector):
        async def correct(self, request):
            self.calls += 1
            assert tuple(item.page_number for item in request.decisions) == (2,)
            corrected = replace(
                await STORAGE.resolve_stored(BATCH_ID, FILE_ID, expected_page_count=4),
                file_id=str(uuid4()),
                path=Path("trusted-corrected.pdf"),
            )
            STORAGE.corrected[corrected.file_id] = corrected
            return corrected

    service._detector = OnePageDetector()
    service._corrector = OnePageCorrector()
    result = await service.reparse(
        OrientationRecoveryCommand("or_" + "x" * 32, (2,))
    )
    assert result.batch_id == RESULT_BATCH_ID
    assert repo.current.selected_pages == (2,)


@pytest.mark.asyncio
async def test_missing_content_write_guard_fails_closed_before_source_open():
    repo = Repo()
    service, guards, detector, corrector, runner = coordinator(repo=repo)

    async def no_guard(*args, **kwargs):
        return None

    guards.acquire_content_write = no_guard
    with pytest.raises(GatewayConflict):
        await public(service).reparse_with_page_orientation(
            OrientationReparseRequest(recovery_token="or_" + "x" * 32)
        )
    assert repo.fail_calls == [(RecoveryState.FAILED, "orientation_content_unavailable")]
    assert STORAGE.events == ["lock-enter", "lock-exit"]
    assert (detector.calls, corrector.calls, runner.calls) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("component", "expected_error", "gateway_error"),
    [
        ("corrector", "orientation_correction_failed", GatewayFailure),
        ("runner", "orientation_pipeline_failed", GatewayUnavailable),
    ],
)
async def test_processing_failure_is_terminal_releases_guard_and_never_retries(
    component, expected_error, gateway_error
):
    repo = Repo()
    service, guards, detector, corrector, runner = coordinator(repo=repo)

    async def broken(*args, **kwargs):
        raise RuntimeError("sensitive body")

    if component == "corrector":
        corrector.correct = broken
    else:
        runner.run = broken
    with pytest.raises(gateway_error) as caught:
        await public(service).reparse_with_page_orientation(
            OrientationReparseRequest(recovery_token="or_" + "x" * 32)
        )
    with pytest.raises(gateway_error):
        await public(service).reparse_with_page_orientation(
            OrientationReparseRequest(recovery_token="or_" + "x" * 32)
        )
    assert "sensitive" not in repr(caught.value)
    assert repo.fail_calls == [(RecoveryState.FAILED, expected_error)]
    assert guards.released == 1
    assert corrector.calls <= 1 and runner.calls <= 1


@pytest.mark.asyncio
async def test_cancellation_releases_live_guard_and_does_not_submit():
    import asyncio

    entered = asyncio.Event()
    unblock = asyncio.Event()

    class BlockingDetector:
        async def detect(self, request):
            entered.set()
            await unblock.wait()

    repo = Repo()
    service, guards, detector, corrector, runner = coordinator(
        repo=repo, detector=BlockingDetector()
    )
    task = asyncio.create_task(
        service.reparse(OrientationRecoveryCommand("or_" + "x" * 32))
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert guards.released == 1
    assert runner.calls == 0
    assert repo.current.state is RecoveryState.CLAIMED


@pytest.mark.asyncio
async def test_progress_callback_failure_never_aborts_or_leaks_into_recovery():
    service, *_ = coordinator()
    calls = 0

    async def broken_progress(value, total):
        nonlocal calls
        calls += 1
        raise RuntimeError("progress transport detail")

    result = await service.reparse(
        OrientationRecoveryCommand("or_" + "x" * 32), progress=broken_progress
    )
    assert result.batch_id == RESULT_BATCH_ID
    assert calls >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "submission",
    [
        pipeline_submission(BATCH_ID),
        pipeline_submission(result_version=4),
        pipeline_submission(accepted_input_file_id=FILE_ID),
        pipeline_submission(accepted_input_sha256="c" * 64),
        pipeline_submission(accepted_input_size_bytes=11),
    ],
)
async def test_runner_must_return_new_batch_and_exact_corrected_version(submission):
    repo = Repo()
    service, guards, detector, corrector, runner = coordinator(repo=repo)

    async def invalid_submission(*args, **kwargs):
        return submission

    runner.run = invalid_submission
    with pytest.raises(GatewayUnavailable):
        await public(service).reparse_with_page_orientation(
            OrientationReparseRequest(recovery_token="or_" + "x" * 32)
        )
    assert repo.fail_calls == [(RecoveryState.FAILED, "orientation_pipeline_failed")]


@pytest.mark.asyncio
async def test_corrected_file_is_re_resolved_and_exactly_matched_before_runner():
    repo = Repo()
    service, guards, detector, corrector, runner = coordinator(repo=repo)
    corrected_id = "44444444-4444-4444-8444-444444444444"

    async def return_forged(request):
        return StoredFile(
            corrected_id,
            Path("attacker-path.pdf"),
            "c" * 64,
            11,
            SupportedMediaType.PDF,
            ".pdf",
            4,
            None,
            None,
        )

    original_resolve = STORAGE.resolve_stored

    async def trusted_resolve(batch_id, file_id, *, expected_page_count, **kwargs):
        if file_id == corrected_id:
            return StoredFile(
                corrected_id,
                Path("trusted-derived.pdf"),
                "d" * 64,
                12,
                SupportedMediaType.PDF,
                ".pdf",
                4,
                None,
                None,
            )
        return await original_resolve(
            batch_id, file_id, expected_page_count=expected_page_count
        )

    corrector.correct = return_forged
    STORAGE.resolve_stored = trusted_resolve
    with pytest.raises(GatewayFailure):
        await public(service).reparse_with_page_orientation(
            OrientationReparseRequest(recovery_token="or_" + "x" * 32)
        )
    assert runner.calls == 0
    assert repo.fail_calls == [(RecoveryState.FAILED, "orientation_correction_failed")]


@pytest.mark.asyncio
async def test_gateway_decorator_delegates_normal_methods_and_owns_only_reparse():
    class Delegate:
        async def upload_document(self, *args, **kwargs): return "upload"
        async def parse_documents(self, *args, **kwargs): return "parse"
        async def get_task_status(self, *args, **kwargs): return "status"
        async def reparse_with_page_orientation(self, *args, **kwargs): raise AssertionError

    class Recovery:
        command = None
        async def reparse(self, request, *, progress=None):
            self.command = request
            return OrientationRecoverySubmission(RESULT_BATCH_ID, BatchStatus.QUEUED, 3)

    recovery = Recovery()
    gateway = OrientationRecoveryGateway(Delegate(), recovery)
    assert await gateway.upload_document(object(), display_name="x", media_type="image/png", content_length=1, idempotency_key=None) == "upload"
    assert await gateway.parse_documents(object()) == "parse"
    assert await gateway.get_task_status(BATCH_ID) == "status"
    response = await gateway.reparse_with_page_orientation(
        OrientationReparseRequest(recovery_token="or_" + "x" * 32)
    )
    assert response.batch_id == RESULT_BATCH_ID
    assert isinstance(recovery.command, OrientationRecoveryCommand)


@pytest.mark.asyncio
async def test_orientation_failure_mapping_is_content_free():
    class InvalidRepo(Repo):
        async def resolve(self, token, *, now):
            raise OrientationFailure(OrientationErrorCode.TOKEN_INVALID, cause=RuntimeError("secret"))

    service, *_ = coordinator(repo=InvalidRepo())
    with pytest.raises(Exception) as caught:
        await public(service).reparse_with_page_orientation(OrientationReparseRequest(recovery_token="or_" + "x" * 32))
    assert "secret" not in repr(caught.value)


def test_recovery_command_repr_redacts_the_raw_token():
    token = "or_" + "private-token" * 3
    command = OrientationRecoveryCommand(token, (2,))
    assert token not in repr(command)
    assert "<redacted>" in repr(command)


@pytest.mark.asyncio
async def test_restart_reconciles_existing_runner_submission_without_duplicate_work():
    repo = Repo(RecoveryState.CLAIMED)

    async def list_claimed(*, now, limit):
        assert (now, limit) == (NOW, 10)
        return (RecoveryClaim("claim-123", "a" * 64, repo.current, False),)

    repo.list_claimed = list_claimed
    service, _, detector, corrector, runner = coordinator(repo=repo)
    runner.reconciled = pipeline_submission()

    result = await service.reconcile_incomplete(limit=10)

    assert (result.scanned, result.completed, result.failed, result.deferred) == (1, 1, 0, 0)
    assert runner.reconcile_calls == ["claim-123"]
    assert repo.current.state is RecoveryState.COMPLETED
    assert (detector.calls, corrector.calls, runner.calls) == (0, 0, 0)


@pytest.mark.asyncio
async def test_runner_takeover_survives_repository_completion_failure_and_restart():
    class FailCompleteOnce(Repo):
        failed_once = False

        async def complete(self, *args, **kwargs):
            if not self.failed_once:
                self.failed_once = True
                raise RuntimeError("database temporarily unavailable")
            return await super().complete(*args, **kwargs)

    repo = FailCompleteOnce()
    service, guards, detector, corrector, runner = coordinator(repo=repo)
    with pytest.raises(RecoveryServiceFailure):
        await service.reparse(
            OrientationRecoveryCommand(recovery_token="or_" + "x" * 32)
        )
    assert repo.current.state is RecoveryState.CLAIMED
    assert (detector.calls, corrector.calls, runner.calls, guards.released) == (
        1,
        1,
        1,
        1,
    )

    repo.list_claimed = lambda **kwargs: _async_value(
        (RecoveryClaim("claim-123", "a" * 64, repo.current, False),)
    )
    reconciled = await service.reconcile_incomplete(limit=1)
    assert (reconciled.completed, reconciled.failed, reconciled.deferred) == (
        1,
        0,
        0,
    )
    assert repo.current.state is RecoveryState.COMPLETED
    assert (detector.calls, corrector.calls, runner.calls) == (1, 1, 1)


@pytest.mark.asyncio
async def test_restart_marks_claim_failed_when_runner_proves_no_submission():
    repo = Repo(RecoveryState.CLAIMED)
    repo.list_claimed = lambda **kwargs: _async_value(
        (RecoveryClaim("claim-123", "a" * 64, repo.current, False),)
    )
    service, _, detector, corrector, runner = coordinator(repo=repo)

    result = await service.reconcile_incomplete(limit=1)

    assert (result.scanned, result.completed, result.failed, result.deferred) == (1, 0, 1, 0)
    assert repo.fail_calls == [
        (RecoveryState.FAILED, "orientation_recovery_interrupted")
    ]
    assert (detector.calls, corrector.calls, runner.calls) == (0, 0, 0)


@pytest.mark.asyncio
async def test_restart_defers_claim_when_runner_reconciliation_is_unavailable():
    repo = Repo(RecoveryState.CLAIMED)
    repo.list_claimed = lambda **kwargs: _async_value(
        (RecoveryClaim("claim-123", "a" * 64, repo.current, False),)
    )
    service, _, detector, corrector, runner = coordinator(repo=repo)

    async def unavailable(_recovery_id):
        raise RuntimeError("backend diagnostic with customer content")

    runner.reconcile = unavailable
    result = await service.reconcile_incomplete(limit=1)

    assert (result.scanned, result.completed, result.failed, result.deferred) == (1, 0, 0, 1)
    assert repo.current.state is RecoveryState.CLAIMED
    assert "customer" not in repr(result)
    assert (detector.calls, corrector.calls, runner.calls) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "submission",
    [
        pipeline_submission(BATCH_ID),
        pipeline_submission(result_version=4),
        object(),
    ],
)
async def test_restart_defers_invalid_or_foreign_runner_result_without_new_work(submission):
    repo = Repo(RecoveryState.CLAIMED)
    repo.list_claimed = lambda **kwargs: _async_value(
        (RecoveryClaim("claim-123", "a" * 64, repo.current, False),)
    )
    service, _, detector, corrector, runner = coordinator(repo=repo)
    runner.reconciled = submission

    result = await service.reconcile_incomplete(limit=1)

    assert (result.completed, result.failed, result.deferred) == (0, 0, 1)
    assert repo.fail_calls == []
    assert repo.current.state is RecoveryState.CLAIMED
    assert (detector.calls, corrector.calls, runner.calls) == (0, 0, 0)


@pytest.mark.asyncio
async def test_restart_cancellation_leaves_claim_for_later_reconciliation():
    repo = Repo(RecoveryState.CLAIMED)
    repo.list_claimed = lambda **kwargs: _async_value(
        (RecoveryClaim("claim-123", "a" * 64, repo.current, False),)
    )
    service, _, detector, corrector, runner = coordinator(repo=repo)
    entered = __import__("asyncio").Event()

    async def blocked(_recovery_id):
        entered.set()
        await __import__("asyncio").Event().wait()

    runner.reconcile = blocked
    task = __import__("asyncio").create_task(service.reconcile_incomplete(limit=1))
    await entered.wait()
    task.cancel()
    with pytest.raises(__import__("asyncio").CancelledError):
        await task
    assert repo.current.state is RecoveryState.CLAIMED
    assert (detector.calls, corrector.calls, runner.calls) == (0, 0, 0)


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_file_storage_resolves_original_from_server_ids_and_revalidates_page_count(tmp_path):
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    writer.write(output)

    async def chunks():
        yield output.getvalue()

    batch_id = str(uuid4())
    storage = FileStorage(tmp_path / "data")
    stored = await storage.store(
        batch_id,
        IncomingFile("source.pdf", "application/pdf", chunks()),
        max_file_size_bytes=1024 * 1024,
        validator=FileValidator(max_pages=500, max_image_pixels=100_000_000),
    )
    resolved = await storage.resolve_stored(
        batch_id, stored.file_id, expected_page_count=2
    )
    assert resolved == stored
    with pytest.raises(FileIntakeFailure):
        await storage.resolve_stored(
            batch_id, stored.file_id, expected_page_count=1
        )


@pytest.mark.asyncio
async def test_file_storage_resolve_rejects_name_substitution_without_deleting_replacement(
    tmp_path, monkeypatch
):
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(output)

    async def chunks():
        yield output.getvalue()

    batch_id = str(uuid4())
    storage = FileStorage(tmp_path / "data")
    stored = await storage.store(
        batch_id,
        IncomingFile("source.pdf", "application/pdf", chunks()),
        max_file_size_bytes=1024 * 1024,
        validator=FileValidator(max_pages=500, max_image_pixels=100_000_000),
    )
    original_validate = FileValidator.validate
    replacement = b"attacker replacement"

    def substitute(self, *args, **kwargs):
        metadata = original_validate(self, *args, **kwargs)
        moved = stored.path.with_suffix(".held")
        stored.path.rename(moved)
        stored.path.write_bytes(replacement)
        return metadata

    monkeypatch.setattr(FileValidator, "validate", substitute)
    with pytest.raises(FileIntakeFailure):
        await storage.resolve_stored(
            batch_id, stored.file_id, expected_page_count=1
        )
    assert stored.path.read_bytes() == replacement


@pytest.mark.asyncio
async def test_file_storage_resolve_rejects_oversized_otherwise_valid_pdf(tmp_path):
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(output)
    payload = output.getvalue() + b"\0" * (30 * 1024 * 1024)

    async def chunks():
        yield payload

    batch_id = str(uuid4())
    storage = FileStorage(tmp_path / "data")
    stored = await storage.store(
        batch_id,
        IncomingFile("source.pdf", "application/pdf", chunks()),
        max_file_size_bytes=len(payload) + 1,
        validator=FileValidator(max_pages=500, max_image_pixels=100_000_000),
    )
    with pytest.raises(FileIntakeFailure):
        await storage.resolve_stored(
            batch_id, stored.file_id, expected_page_count=1
        )


@pytest.mark.asyncio
async def test_file_storage_resolve_rejects_hardlink_added_during_validation(
    tmp_path, monkeypatch
):
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(output)

    async def chunks():
        yield output.getvalue()

    batch_id = str(uuid4())
    storage = FileStorage(tmp_path / "data")
    stored = await storage.store(
        batch_id,
        IncomingFile("source.pdf", "application/pdf", chunks()),
        max_file_size_bytes=1024 * 1024,
        validator=FileValidator(max_pages=500, max_image_pixels=100_000_000),
    )
    hardlink = stored.path.with_suffix(".linked")
    original_validate = FileValidator.validate

    def add_link(self, *args, **kwargs):
        metadata = original_validate(self, *args, **kwargs)
        hardlink.hardlink_to(stored.path)
        return metadata

    monkeypatch.setattr(FileValidator, "validate", add_link)
    with pytest.raises(FileIntakeFailure):
        await storage.resolve_stored(
            batch_id, stored.file_id, expected_page_count=1
        )
    assert hardlink.exists()
