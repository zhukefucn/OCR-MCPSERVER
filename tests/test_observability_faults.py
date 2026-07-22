from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import threading
import time

import pytest
from fastapi.testclient import TestClient

from ocr_mcp_server.app import create_app
from ocr_mcp_server.domain import (
    CandidateReference,
    FileStatus,
    ImageCandidate,
    MinerUImageFormat,
    OrthogonalAngle,
    ProcessingStage,
    SecondaryOCREngine,
    SecondaryOcrFailure,
    SecondaryOcrResult,
    SecondaryResultKind,
    SecondaryResultState,
)
from ocr_mcp_server.domain.files import StoredFile, SupportedMediaType
from ocr_mcp_server.domain.orientation import (
    OrientationDecision,
    RecoveryClaim,
    RecoverySnapshot,
    RecoveryState,
)
from ocr_mcp_server.domain.retention import ContentWriteGuard
from ocr_mcp_server.domain.tasks import FileTaskSnapshot, LeaseClaim
from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker
from ocr_mcp_server.services.observability import NullObservability, RecoveryOutcome
from ocr_mcp_server.services.orchestration import (
    OrchestrationService,
    PipelineResult,
)
from ocr_mcp_server.services.orientation_recovery import (
    OrientationRecoveryCommand,
    OrientationRecoveryCoordinator,
    RecoveryServiceFailure,
)
from ocr_mcp_server.settings import AppSettings, OrchestrationSettings


class RaisingSink:
    def __getattr__(self, name):
        if name.startswith(("observe_", "set_")):
            def raise_observation(*args, **kwargs):
                raise RuntimeError("private OCR text C:/customer/document.pdf")
            return raise_observation
        raise AttributeError(name)


class CostlySink:
    """Synchronous deterministic work without timing sleeps or scheduler races."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith(("observe_", "set_")):
            def observe(*args):
                checksum = 0
                for number in range(2000):
                    checksum ^= number
                self.calls.append((name, args, checksum))
            return observe
        raise AttributeError(name)


class BlockingSink:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def __getattr__(self, name):
        if name.startswith(("observe_", "set_")):
            def block(*args):
                self.entered.set()
                self.release.wait()
            return block
        raise AttributeError(name)


class CancelledErrorSink:
    def __getattr__(self, name):
        if name.startswith(("observe_", "set_")):
            def cancel(*args):
                raise asyncio.CancelledError("sink-originated")
            return cancel
        raise AttributeError(name)


NOW = datetime(2026, 7, 23, tzinfo=UTC)


def _claim(file_id: str) -> LeaseClaim:
    snapshot = FileTaskSnapshot(
        id=file_id,
        batch_id="batch",
        position=0,
        status=FileStatus.PROCESSING,
        stage=ProcessingStage.QUEUED,
        progress=12,
        attempt_count=1,
        max_attempts=2,
        lease_owner="worker",
        lease_token="lease",
        lease_expires_at=NOW + timedelta(seconds=30),
        last_error_code=None,
        created_at=NOW,
        updated_at=NOW,
        version=2,
    )
    return LeaseClaim(snapshot, "lease", NOW + timedelta(seconds=30))


class StatefulTaskRepository:
    def __init__(self, claim):
        self.current = claim.file

    async def update_progress(self, file_id, lease_token, *, stage, counters, now):
        self.current = replace(
            self.current, stage=stage, version=self.current.version + 1
        )
        return self.current

    async def complete_file(self, file_id, lease_token, *, with_warnings, now):
        self.current = replace(
            self.current,
            status=FileStatus.COMPLETED,
            stage=ProcessingStage.COMPLETED,
            progress=100,
            version=self.current.version + 1,
        )
        return self.current


class ProgressPipeline:
    async def run(self, file, progress, cancellation):
        await progress.report(ProcessingStage.MINERU_PARSING)
        await progress.report(ProcessingStage.MINERU_PARSING)
        return PipelineResult.success()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sink", [RaisingSink(), CancelledErrorSink(), CostlySink(), BlockingSink()]
)
async def test_claimed_task_and_stage_sink_faults_preserve_terminal_state(sink):
    canary = "OCR text private.pdf https://private.test C:\\private Authorization token"
    claim = _claim(canary)
    repository = StatefulTaskRepository(claim)
    service = OrchestrationService(
        repository,
        ProgressPipeline(),
        OrchestrationSettings(),
        worker_identity="worker",
        observability=sink,
    )
    started = time.monotonic()
    await service._execute_claim(claim)
    assert time.monotonic() - started < 0.2
    assert repository.current.status is FileStatus.COMPLETED
    assert canary not in repr(getattr(sink, "calls", ()))
    await service.close()
    if isinstance(sink, BlockingSink):
        sink.release.set()


@pytest.mark.asyncio
async def test_raising_queue_observations_do_not_change_lifecycle_or_saturation():
    orchestration = OrchestrationService(
        object(), object(), OrchestrationSettings(wake_queue_capacity=1),
        worker_identity="worker", observability=RaisingSink(),
    )
    assert orchestration.notify_work() is True
    assert orchestration.notify_work() is False
    await orchestration.close()

    worker = SingleOwnerSecondaryOcrWorker(
        lambda: object(), queue_capacity=1, observability=RaisingSink()
    )
    await worker.close()
    assert worker.queue_depth == 0


def _candidate(tmp_path: Path, name: str) -> ImageCandidate:
    path = tmp_path / f"{name}.png"
    path.write_bytes(b"opaque")
    return ImageCandidate(
        candidate_id=name,
        file_task_id="task",
        result_version=1,
        sha256=("a" if name == "one" else "b") * 64,
        size_bytes=6,
        image_format=MinerUImageFormat.PNG,
        width=1,
        height=1,
        primary_path=path,
        alias_paths=(path,),
        references=(CandidateReference.standalone_input(),),
        node_type_hints=(),
    )


def _ocr_result() -> SecondaryOcrResult:
    return SecondaryOcrResult(
        kind=SecondaryResultKind.OTHER,
        angle=OrthogonalAngle.DEG_0,
        content=None,
        content_format=None,
        confidence=0.9,
        engine=SecondaryOCREngine.PP_STRUCTURE_V3,
        model_versions={"pipeline": "test"},
        state=SecondaryResultState.VALID,
    )


@pytest.mark.asyncio
async def test_paddle_fault_sink_preserves_fifo_cancel_saturation_backend_failure_and_close(
    tmp_path: Path,
) -> None:
    entered, release = threading.Event(), threading.Event()
    order = []

    class Backend:
        def recognize(self, candidate):
            order.append(candidate.candidate_id)
            if candidate.candidate_id == "one":
                entered.set()
                assert release.wait(3)
            if candidate.candidate_id == "bad":
                raise RuntimeError("private backend exception OCR text")
            return _ocr_result()

        def close(self):
            order.append("close")

    observation_sink = BlockingSink()
    worker = SingleOwnerSecondaryOcrWorker(
        Backend, queue_capacity=1, observability=observation_sink
    )
    await worker.start()
    first = asyncio.create_task(worker.recognize(_candidate(tmp_path, "one")))
    assert await asyncio.to_thread(entered.wait, 2)
    second = asyncio.create_task(worker.recognize(_candidate(tmp_path, "two")))
    await asyncio.sleep(0)
    with pytest.raises(SecondaryOcrFailure):
        await worker.recognize(_candidate(tmp_path, "three"))
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    release.set()
    await first
    bad = asyncio.create_task(worker.recognize(_candidate(tmp_path, "bad")))
    with pytest.raises(SecondaryOcrFailure):
        await bad
    assert (
        await worker.recognize(_candidate(tmp_path, "four"))
    ).kind is SecondaryResultKind.OTHER
    await worker.close()
    observation_sink.release.set()
    assert order == ["one", "two", "bad", "four", "close"]
    assert worker.queue_depth == 0


class UncertainRecoveryRepository:
    def __init__(self):
        self.marker = False
        self.failures = 0
        self.snapshot = RecoverySnapshot(
            file_id="22222222-2222-4222-8222-222222222222",
            batch_id="11111111-1111-4111-8111-111111111111",
            source_result_version=1,
            page_count=1,
            suspected_pages=(1,),
            selected_pages=None,
            expires_at=NOW + timedelta(hours=1),
            state=RecoveryState.ISSUED,
            request_fingerprint=None,
            claim_id=None,
            corrected_input_version=None,
            corrected_input_file_id=None,
            corrected_input_sha256=None,
            corrected_input_size_bytes=None,
            result_batch_id=None,
            result_version=None,
            error_code=None,
            version=1,
        )

    async def resolve(self, token, *, now):
        return self.snapshot

    async def claim(self, token, pages, *, now):
        acquired = self.snapshot.state is RecoveryState.ISSUED
        if acquired:
            self.snapshot = replace(
                self.snapshot,
                state=RecoveryState.CLAIMED,
                selected_pages=(1,),
                request_fingerprint="a" * 64,
                claim_id="claim-abc",
                version=2,
            )
        return RecoveryClaim("claim-abc", "a" * 64, self.snapshot, acquired)

    async def fail(self, claim, *, state, error_code, now):
        self.failures += 1
        self.snapshot = replace(
            self.snapshot, state=state, error_code=error_code, version=3
        )
        return self.snapshot

    async def mark_terminal_observed(self, claim_id):
        if self.marker:
            return False
        self.marker = True
        return True


class UncertainStorage:
    @asynccontextmanager
    async def batch_lock(self, *args, **kwargs):
        yield None

    async def resolve_stored(self, batch_id, file_id, *, expected_page_count):
        return StoredFile(
            file_id=file_id,
            path=Path("server.pdf"),
            sha256="b" * 64,
            size_bytes=1,
            media_type=SupportedMediaType.PDF,
            extension=".pdf",
            page_count=1,
            width=None,
            height=None,
        )


class RecoveryGuards:
    async def acquire_content_write(
        self,
        batch_id,
        writer_id,
        *,
        now,
        lease_seconds,
        allow_missing=False,
    ):
        return ContentWriteGuard(
            batch_id, writer_id, "guard", now + timedelta(seconds=lease_seconds)
        )

    async def release_content_write(self, guard):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("sink", [RaisingSink(), CostlySink(), BlockingSink()])
async def test_recovery_terminal_sink_fault_is_once_only_and_replay_stable(sink):
    repository = UncertainRecoveryRepository()

    class Detector:
        async def detect(self, request):
            return (
                OrientationDecision(
                    1, OrthogonalAngle.DEG_0, 0.99, "metadata", True
                ),
            )

    def coordinator():
        return OrientationRecoveryCoordinator(
            repository=repository,
            detector=Detector(),
            corrector=object(),
            runner=object(),
            storage=UncertainStorage(),
            marker_registry=object(),
            content_write_guards=RecoveryGuards(),
            now_factory=lambda: NOW,
            observability=sink,
        )

    command = OrientationRecoveryCommand(recovery_token="raw-token-private.pdf")
    for service in (coordinator(), coordinator()):
        with pytest.raises(RecoveryServiceFailure):
            await service.reparse(command)
        service.drain_observations()
        service.close_observability()
    assert repository.failures == 1
    assert repository.marker is True
    calls = getattr(sink, "calls", ())
    observed = [args[0] for name, args, _ in calls if name == "observe_recovery"]
    if isinstance(sink, CostlySink):
        assert observed == [RecoveryOutcome.UNCERTAIN]
    else:
        assert observed == []
    assert "raw-token-private.pdf" not in repr(calls)
    if isinstance(sink, BlockingSink):
        sink.release.set()


def test_raising_http_sink_preserves_rest_and_mcp_auth_responses():
    settings = AppSettings(auth={"api_keys": ["a-secure-api-key-0000000000000001"]})
    outputs = []
    for sink in (
        NullObservability(),
        RaisingSink(),
        CancelledErrorSink(),
        CostlySink(),
    ):
        with TestClient(create_app(settings, observability=sink)) as client:
            live = client.get("/health/live")
            mcp = client.post(
                "/mcp",
                headers={"Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
        outputs.append((live.status_code, live.json(), mcp.status_code, mcp.json()))
    assert outputs[0] == outputs[1] == outputs[2] == outputs[3]


def test_forever_blocking_sink_does_not_delay_live_rest_or_readiness():
    sink = BlockingSink()
    settings = AppSettings(auth={"api_keys": []})
    with TestClient(create_app(settings, observability=sink)) as client:
        started = time.monotonic()
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        missing = client.get("/v1/tasks/00000000-0000-4000-8000-000000000000")
        elapsed = time.monotonic() - started
    sink.release.set()
    assert elapsed < 0.2
    assert live.status_code == 200
    assert ready.status_code == 503
    assert missing.status_code in {404, 503}


@pytest.mark.asyncio
async def test_exactly_three_mcp_tools_and_recovery_schema_stay_curated():
    from fastmcp import Client
    from ocr_mcp_server.api.mcp import create_mcp_server

    async with Client(create_mcp_server(None)) as client:
        tools = await client.list_tools()
    assert len(tools) == 3
    recovery = next(
        tool for tool in tools if tool.name == "reparse_with_page_orientation"
    )
    assert set(recovery.inputSchema["properties"]) == {"recovery_token", "pages"}


def test_production_surface_contains_no_fault_injection_controls():
    root = Path(__file__).parents[1] / "src" / "ocr_mcp_server"
    text = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.py"))
    forbidden = ("fault_injection", "inject_fault", "OCR_FAULT", "--fault")
    assert all(term not in text for term in forbidden)
