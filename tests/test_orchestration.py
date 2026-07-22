from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from ocr_mcp_server.domain.errors import LeaseConflictError, PersistenceError
from ocr_mcp_server.domain.models import BatchStatus, FileStatus, ProcessingStage
from ocr_mcp_server.domain.progress import ProgressCounters, ProgressUnit
from ocr_mcp_server.infra.database import (
    create_database_engine,
    create_session_factory,
    initialize_schema,
)
from ocr_mcp_server.infra.task_repository import TaskRepository
from ocr_mcp_server.services.orchestration import (
    FilePipeline,
    OrchestrationService,
    PipelineCancellation,
    PipelineErrorCode,
    PipelineFailure,
    PipelineFileIdentity,
    PipelineResult,
    ProgressNotification,
    ProgressReporter,
    ServiceLifecycleError,
)
from ocr_mcp_server.services.observability import StageOutcome, TaskOutcome
from ocr_mcp_server.settings import OrchestrationSettings


class RecordingObservability:
    def __init__(self) -> None:
        self.queue_depths = []
        self.tasks = []
        self.stages = []

    def set_orchestration_queue_depth(self, depth):
        self.queue_depths.append(depth)

    def observe_task(self, outcome, duration):
        self.tasks.append((outcome, duration))

    def observe_stage(self, stage, outcome, duration):
        self.stages.append((stage, outcome, duration))


class ManualClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 1, tzinfo=UTC)
        self.elapsed = 0.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.elapsed

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.elapsed + seconds, future))
        await future

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)
        self.elapsed += seconds
        pending = self._sleepers
        self._sleepers = []
        for deadline, future in pending:
            if deadline <= self.elapsed and not future.done():
                future.set_result(None)
            elif not future.done():
                self._sleepers.append((deadline, future))


class CallablePipeline(FilePipeline):
    def __init__(
        self,
        callback: Callable[
            [PipelineFileIdentity, ProgressReporter, PipelineCancellation],
            Awaitable[PipelineResult],
        ],
    ) -> None:
        self.callback = callback

    async def run(
        self,
        file: PipelineFileIdentity,
        progress: ProgressReporter,
        cancellation: PipelineCancellation,
    ) -> PipelineResult:
        return await self.callback(file, progress, cancellation)


async def wait_until(predicate: Callable[[], Awaitable[bool]]) -> None:
    for _ in range(200):
        if await predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


@pytest.mark.asyncio
async def test_wake_queue_gauge_uses_replacement_semantics_and_close_drains() -> None:
    observations = RecordingObservability()
    service = OrchestrationService(
        object(), object(), OrchestrationSettings(wake_queue_capacity=1),
        worker_identity="worker", observability=observations,
    )
    assert service.notify_work() is True
    assert service.notify_work() is False
    await service.close()
    assert observations.queue_depths == [1, 1, 0]


@pytest_asyncio.fixture
async def orchestration_repository(tmp_path: Path):
    engine = create_database_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'orchestration.sqlite3').as_posix()}",
        busy_timeout_ms=5000,
    )
    await initialize_schema(engine)
    repository = TaskRepository(create_session_factory(engine))
    try:
        yield repository, engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_success_progress_is_persisted_before_throttled_notifications(
    orchestration_repository,
) -> None:
    repository, _ = orchestration_repository
    created = await repository.create_batch("success", ["file-a"])
    clock = ManualClock()

    class CheckingSink:
        def __init__(self) -> None:
            self.items: list[ProgressNotification] = []

        async def publish(self, notification: ProgressNotification) -> None:
            persisted = await repository.get_file(notification.file_id)
            assert persisted is not None
            assert persisted.version == notification.version
            assert persisted.progress == notification.progress
            self.items.append(notification)

    sink = CheckingSink()
    observations = RecordingObservability()

    async def run_pipeline(file, progress, cancellation):
        assert file.file_id == "file-a"
        cancellation.checkpoint()
        await progress.report(
            ProcessingStage.MINERU_PARSING,
            ProgressCounters(1, 2, ProgressUnit.PAGES),
        )
        await progress.report(
            ProcessingStage.MINERU_PARSING,
            ProgressCounters(2, 4, ProgressUnit.PAGES),
        )
        clock.advance(2)
        await progress.report(
            ProcessingStage.MINERU_PARSING,
            ProgressCounters(3, 4, ProgressUnit.PAGES),
        )
        await progress.report(
            ProcessingStage.MERGING,
            ProgressCounters(0, 1, ProgressUnit.ITEMS),
        )
        return PipelineResult.success_with_warnings()

    service = OrchestrationService(
        repository,
        CallablePipeline(run_pipeline),
        OrchestrationSettings(),
        notification_sink=sink,
        clock=clock,
        worker_identity="worker",
        observability=observations,
    )
    await service.start()

    async def terminal() -> bool:
        return (
            (await repository.get_batch(created.batch.id)).status
            is BatchStatus.COMPLETED
            and bool(sink.items)
            and sink.items[-1].status is FileStatus.COMPLETED_WITH_WARNINGS
        )

    await wait_until(terminal)
    await service.close()

    persisted = await repository.get_file("file-a")
    assert persisted.status is FileStatus.COMPLETED_WITH_WARNINGS
    assert persisted.progress == 100
    assert [item.stage for item in sink.items] == [
        ProcessingStage.QUEUED,
        ProcessingStage.MINERU_PARSING,
        ProcessingStage.MINERU_PARSING,
        ProcessingStage.MERGING,
        ProcessingStage.COMPLETED_WITH_WARNINGS,
    ]
    assert [item.version for item in sink.items] == sorted(
        item.version for item in sink.items
    )
    assert [item.progress for item in sink.items] == sorted(
        item.progress for item in sink.items
    )
    assert service.notification_tracking_size == 0
    assert observations.tasks == [(TaskOutcome.WARNING, 2.0)]
    assert [(stage, outcome) for stage, outcome, _ in observations.stages] == [
        (ProcessingStage.QUEUED, StageOutcome.COMPLETED),
        (ProcessingStage.MINERU_PARSING, StageOutcome.COMPLETED),
        (ProcessingStage.MERGING, StageOutcome.COMPLETED),
    ]
    assert [duration for _, _, duration in observations.stages] == [0.0, 2.0, 0.0]


@pytest.mark.asyncio
async def test_retry_unknown_and_nonretryable_failures_are_safe_and_siblings_continue(
    orchestration_repository,
) -> None:
    repository, _ = orchestration_repository
    created = await repository.create_batch(
        "failures", ["retry", "exhausted", "typed", "unknown", "success"], max_attempts=2
    )
    calls: defaultdict[str, int] = defaultdict(int)
    secret = "C:/private/client.pdf recognized customer account 123"

    async def run_pipeline(file, progress, cancellation):
        calls[file.file_id] += 1
        if file.file_id == "retry" and calls[file.file_id] == 1:
            await progress.report(
                ProcessingStage.MERGING,
                ProgressCounters(1, 2, ProgressUnit.ITEMS),
            )
            raise PipelineFailure(
                PipelineErrorCode.DEPENDENCY_UNAVAILABLE,
                retryable=True,
                cause=RuntimeError(secret),
            )
        if file.file_id == "retry" and calls[file.file_id] == 2:
            resumed = await progress.report(
                ProcessingStage.MINERU_PARSING,
                ProgressCounters(1, 10, ProgressUnit.PAGES),
            )
            assert resumed.progress == 91
        if file.file_id == "exhausted":
            raise PipelineFailure(
                PipelineErrorCode.DEPENDENCY_UNAVAILABLE,
                retryable=True,
                cause=RuntimeError(secret),
            )
        if file.file_id == "typed":
            raise PipelineFailure(
                PipelineErrorCode.INPUT_INVALID,
                retryable=False,
                cause=RuntimeError(secret),
            )
        if file.file_id == "unknown":
            raise RuntimeError(secret)
        return PipelineResult.success()

    service = OrchestrationService(
        repository,
        CallablePipeline(run_pipeline),
        OrchestrationSettings(),
        worker_identity="worker",
    )
    await service.start()

    async def terminal() -> bool:
        status = (await repository.get_batch(created.batch.id)).status
        return status is BatchStatus.COMPLETED_WITH_ERRORS

    await wait_until(terminal)
    await service.close()
    files = await repository.list_batch_files(created.batch.id)

    assert calls == {
        "retry": 2,
        "exhausted": 2,
        "typed": 1,
        "unknown": 1,
        "success": 1,
    }
    assert [item.status for item in files] == [
        FileStatus.COMPLETED,
        FileStatus.FAILED,
        FileStatus.FAILED,
        FileStatus.FAILED,
        FileStatus.COMPLETED,
    ]
    assert files[1].attempt_count == 2
    assert files[2].attempt_count == 1
    assert files[2].last_error_code == PipelineErrorCode.INPUT_INVALID.value
    assert files[3].last_error_code == PipelineErrorCode.UNEXPECTED.value
    retry_events = [
        event
        for event in await repository.list_batch_events(created.batch.id)
        if event.file_id == "retry"
    ]
    assert any(
        event.new_stage is ProcessingStage.MINERU_PARSING
        and event.new_progress == 91
        for event in retry_events
    )
    assert secret not in repr(files)
    assert secret not in repr(await repository.list_batch_events(created.batch.id))


@pytest.mark.asyncio
async def test_queue_saturation_and_duplicate_wakes_never_lose_or_duplicate_durable_work(
    orchestration_repository,
) -> None:
    repository, _ = orchestration_repository
    first = await repository.create_batch("first", ["file-a"])
    entered = asyncio.Event()
    release = asyncio.Event()
    active: defaultdict[str, int] = defaultdict(int)
    maximum: defaultdict[str, int] = defaultdict(int)
    calls: defaultdict[str, int] = defaultdict(int)

    async def run_pipeline(file, progress, cancellation):
        calls[file.file_id] += 1
        active[file.file_id] += 1
        maximum[file.file_id] = max(maximum[file.file_id], active[file.file_id])
        try:
            if file.file_id == "file-a":
                entered.set()
                await release.wait()
            return PipelineResult.success()
        finally:
            active[file.file_id] -= 1

    settings = OrchestrationSettings(
        worker_count=3,
        wake_queue_capacity=1,
    )
    service = OrchestrationService(
        repository,
        CallablePipeline(run_pipeline),
        settings,
        worker_identity="worker",
    )
    await service.start()
    await entered.wait()
    second = await repository.create_batch("second", ["file-b"])
    wake_results = [service.notify_work() for _ in range(20)]
    release.set()

    async def both_terminal() -> bool:
        statuses = [
            (await repository.get_batch(batch.batch.id)).status
            for batch in (first, second)
        ]
        return statuses == [BatchStatus.COMPLETED, BatchStatus.COMPLETED]

    await wait_until(both_terminal)
    await service.close()

    assert False in wake_results
    assert calls == {"file-a": 1, "file-b": 1}
    assert maximum == {"file-a": 1, "file-b": 1}


@pytest.mark.asyncio
async def test_startup_and_periodic_recovery_handle_expiry_and_exhaustion(
    orchestration_repository,
) -> None:
    repository, _ = orchestration_repository
    clock = ManualClock()
    recoverable = await repository.create_batch(
        "recoverable", ["recoverable-file"], max_attempts=2
    )
    exhausted = await repository.create_batch(
        "exhausted", ["exhausted-file"], max_attempts=1
    )
    await repository.claim_next("dead-worker", now=clock.now(), lease_seconds=5)
    await repository.claim_next("dead-worker", now=clock.now(), lease_seconds=5)
    clock.advance(6)
    calls: list[str] = []

    async def run_pipeline(file, progress, cancellation):
        calls.append(file.file_id)
        return PipelineResult.success()

    service = OrchestrationService(
        repository,
        CallablePipeline(run_pipeline),
        OrchestrationSettings(
            lease_seconds=10,
            heartbeat_seconds=2,
            recovery_scan_seconds=5,
        ),
        clock=clock,
        worker_identity="restarted-worker",
    )
    await service.start()

    async def recovered_terminal() -> bool:
        return (
            (await repository.get_batch(recoverable.batch.id)).status
            is BatchStatus.COMPLETED
            and (await repository.get_batch(exhausted.batch.id)).status
            is BatchStatus.FAILED
        )

    await wait_until(recovered_terminal)
    await service.close()

    assert calls == ["recoverable-file"]
    assert (await repository.get_file("recoverable-file")).attempt_count == 2
    assert (await repository.get_file("exhausted-file")).last_error_code == "lease_expired"


@pytest.mark.asyncio
async def test_startup_recovery_emits_committed_requeue_and_terminal_notifications_even_if_sink_fails(
    orchestration_repository,
) -> None:
    repository, _ = orchestration_repository
    clock = ManualClock()
    await repository.create_batch("startup-requeue", ["requeue-file"], max_attempts=2)
    await repository.create_batch("startup-terminal", ["failed-file"], max_attempts=1)
    await repository.claim_next("dead-worker", now=clock.now(), lease_seconds=5)
    await repository.claim_next("dead-worker", now=clock.now(), lease_seconds=5)
    clock.advance(6)
    secret = "C:/private/recovery-notification.pdf"

    class FailingCheckingSink:
        def __init__(self) -> None:
            self.items: list[ProgressNotification] = []

        async def publish(self, notification: ProgressNotification) -> None:
            persisted = await repository.get_file(notification.file_id)
            assert persisted is not None
            assert persisted.version == notification.version
            assert persisted.status is notification.status
            self.items.append(notification)
            raise RuntimeError(secret)

    sink = FailingCheckingSink()

    async def run_pipeline(file, progress, cancellation):
        return PipelineResult.success()

    service = OrchestrationService(
        repository,
        CallablePipeline(run_pipeline),
        OrchestrationSettings(),
        notification_sink=sink,
        clock=clock,
        worker_identity="restarted-worker",
    )
    await service.start()

    assert {
        item.file_id: (item.status, item.stage, item.error_code)
        for item in sink.items
    } == {
        "requeue-file": (
            FileStatus.QUEUED,
            ProcessingStage.QUEUED,
            "lease_expired",
        ),
        "failed-file": (
            FileStatus.FAILED,
            ProcessingStage.FAILED,
            "lease_expired",
        ),
    }
    assert secret not in repr(sink.items)
    await service.close()


@pytest.mark.asyncio
async def test_periodic_recovery_emits_requeue_before_the_recovered_file_is_reclaimed(
    orchestration_repository,
) -> None:
    repository, _ = orchestration_repository
    clock = ManualClock()
    await repository.create_batch("periodic-notification", ["file-a"], max_attempts=2)
    await repository.claim_next("dead-worker", now=clock.now(), lease_seconds=5)
    entered = asyncio.Event()
    release = asyncio.Event()
    recovery_seen = asyncio.Event()

    class CheckingSink:
        def __init__(self) -> None:
            self.items: list[ProgressNotification] = []

        async def publish(self, notification: ProgressNotification) -> None:
            self.items.append(notification)
            if notification.status is FileStatus.QUEUED:
                recovery_seen.set()

    sink = CheckingSink()

    async def run_pipeline(file, progress, cancellation):
        entered.set()
        await release.wait()
        return PipelineResult.success()

    service = OrchestrationService(
        repository,
        CallablePipeline(run_pipeline),
        OrchestrationSettings(
            lease_seconds=10,
            heartbeat_seconds=2,
            idle_poll_seconds=100,
            recovery_scan_seconds=5,
        ),
        notification_sink=sink,
        clock=clock,
        worker_identity="worker",
    )
    await service.start()

    async def recovery_timer_is_armed() -> bool:
        return any(deadline == 5 for deadline, _future in clock._sleepers)

    await wait_until(recovery_timer_is_armed)
    clock.advance(6)

    await recovery_seen.wait()
    await entered.wait()
    statuses = [item.status for item in sink.items]
    assert statuses.index(FileStatus.QUEUED) < statuses.index(FileStatus.PROCESSING)
    recovery_notification = next(
        item for item in sink.items if item.status is FileStatus.QUEUED
    )
    events = await repository.list_batch_events(recovery_notification.batch_id)
    assert any(
        event.version == recovery_notification.version
        and event.new_status is FileStatus.QUEUED
        and event.error_code == "lease_expired"
        for event in events
    )
    release.set()
    await service.close()


@pytest.mark.asyncio
async def test_idle_poll_and_periodic_recovery_find_work_without_wake_tokens(
    orchestration_repository,
) -> None:
    repository, _ = orchestration_repository
    clock = ManualClock()
    calls: list[str] = []

    async def run_pipeline(file, progress, cancellation):
        calls.append(file.file_id)
        return PipelineResult.success()

    service = OrchestrationService(
        repository,
        CallablePipeline(run_pipeline),
        OrchestrationSettings(
            lease_seconds=10,
            heartbeat_seconds=2,
            idle_poll_seconds=1,
            recovery_scan_seconds=5,
        ),
        clock=clock,
        worker_identity="worker",
    )
    await service.start()
    await asyncio.sleep(0)
    durable_only = await repository.create_batch("durable-only", ["file-a"])
    clock.advance(1)

    async def first_terminal() -> bool:
        return (
            (await repository.get_batch(durable_only.batch.id)).status
            is BatchStatus.COMPLETED
        )

    await wait_until(first_terminal)
    periodic = await repository.create_batch("periodic", ["file-b"])
    await repository.claim_next("dead-worker", now=clock.now(), lease_seconds=2)
    clock.advance(6)

    async def second_terminal() -> bool:
        return (
            (await repository.get_batch(periodic.batch.id)).status
            is BatchStatus.COMPLETED
        )

    await wait_until(second_terminal)
    await service.close()
    assert calls == ["file-a", "file-b"]


@pytest.mark.asyncio
async def test_shutdown_leaves_interrupted_work_for_expiry_and_never_repeats_terminal_work(
    orchestration_repository,
) -> None:
    repository, _ = orchestration_repository
    clock = ManualClock()
    completed = await repository.create_batch("already-complete", ["complete-file"])
    claim = await repository.claim_next("setup", now=clock.now(), lease_seconds=30)
    await repository.complete_file(
        claim.file.id,
        claim.lease_token,
        with_warnings=False,
        now=clock.now(),
    )
    interrupted = await repository.create_batch("interrupted", ["interrupted-file"])
    entered = asyncio.Event()
    calls: list[str] = []

    async def run_pipeline(file, progress, cancellation):
        calls.append(file.file_id)
        entered.set()
        await asyncio.Event().wait()

    service = OrchestrationService(
        repository,
        CallablePipeline(run_pipeline),
        OrchestrationSettings(lease_seconds=5, heartbeat_seconds=2),
        clock=clock,
        worker_identity="worker",
    )
    await service.start()
    await entered.wait()
    await service.close()

    persisted = await repository.get_file(interrupted.files[0].id)
    assert calls == ["interrupted-file"]
    assert persisted.status is FileStatus.PROCESSING
    assert persisted.status is not FileStatus.CANCELLED
    clock.advance(6)
    assert len(await repository.recover_expired_leases(now=clock.now())) == 1
    assert (await repository.get_file("interrupted-file")).status is FileStatus.QUEUED
    assert (await repository.get_batch(completed.batch.id)).status is BatchStatus.COMPLETED


@pytest.mark.asyncio
async def test_heartbeat_extends_lease_and_lease_loss_cancels_pipeline_without_terminal_write(
    orchestration_repository,
) -> None:
    repository, _ = orchestration_repository
    await repository.create_batch("lease", ["file-a"])
    clock = ManualClock()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def run_pipeline(file, progress, cancellation):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    service = OrchestrationService(
        repository,
        CallablePipeline(run_pipeline),
        OrchestrationSettings(
            lease_seconds=5,
            heartbeat_seconds=2,
            recovery_scan_seconds=30,
        ),
        clock=clock,
        worker_identity="worker",
    )
    await service.start()
    await entered.wait()
    initial = await repository.get_file("file-a")
    clock.advance(2)

    async def heartbeat_seen() -> bool:
        current = await repository.get_file("file-a")
        return current.version > initial.version

    await wait_until(heartbeat_seen)
    renewed = await repository.get_file("file-a")
    assert renewed.lease_expires_at == clock.now() + timedelta(seconds=5)

    await repository.recover_expired_leases(
        now=renewed.lease_expires_at + timedelta(seconds=1)
    )
    clock.advance(2)
    await cancelled.wait()
    persisted = await repository.get_file("file-a")
    assert persisted.status is FileStatus.QUEUED
    assert persisted.progress == 12
    await service.close()
    assert (await repository.get_file("file-a")).status is not FileStatus.CANCELLED


@pytest.mark.asyncio
async def test_sink_failure_and_cancellation_fall_back_to_polling_and_lifecycle_is_safe(
    orchestration_repository,
) -> None:
    repository, engine = orchestration_repository
    created = await repository.create_batch("sink", ["file-a"])

    class CancellingSink:
        async def publish(self, notification: ProgressNotification) -> None:
            raise asyncio.CancelledError("private notification body")

    async def run_pipeline(file, progress, cancellation):
        return PipelineResult.success()

    service = OrchestrationService(
        repository,
        CallablePipeline(run_pipeline),
        OrchestrationSettings(),
        notification_sink=CancellingSink(),
        worker_identity="worker",
    )
    await service.start()
    with pytest.raises(ServiceLifecycleError):
        await service.start()

    async def terminal() -> bool:
        return (await repository.get_batch(created.batch.id)).status is BatchStatus.COMPLETED

    await wait_until(terminal)
    await service.close()
    await service.close()

    assert service.notify_work() is False
    async with engine.connect() as connection:
        assert (await connection.exec_driver_sql("SELECT 1")).scalar_one() == 1


@pytest.mark.asyncio
async def test_transient_claim_persistence_error_returns_worker_to_idle_poll_without_leaking(
    orchestration_repository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository, _ = orchestration_repository
    created = await repository.create_batch("transient-claim", ["file-a"])
    clock = ManualClock()
    first_failure_seen = asyncio.Event()
    pipeline_entered = asyncio.Event()
    secret = "C:/private/customer.pdf recognized account text"

    class OneShotFailingRepository:
        def __init__(self, wrapped: TaskRepository) -> None:
            self.wrapped = wrapped
            self.claim_attempts = 0

        def __getattr__(self, name: str):
            return getattr(self.wrapped, name)

        async def claim_next(self, *args, **kwargs):
            self.claim_attempts += 1
            if self.claim_attempts == 1:
                first_failure_seen.set()
                raise PersistenceError(cause=RuntimeError(secret))
            return await self.wrapped.claim_next(*args, **kwargs)

    flaky_repository = OneShotFailingRepository(repository)

    async def run_pipeline(file, progress, cancellation):
        pipeline_entered.set()
        return PipelineResult.success()

    service = OrchestrationService(
        flaky_repository,  # type: ignore[arg-type]
        CallablePipeline(run_pipeline),
        OrchestrationSettings(idle_poll_seconds=1),
        clock=clock,
        worker_identity="worker",
    )
    await service.start()
    await first_failure_seen.wait()

    async def idle_timer_is_armed() -> bool:
        return any(deadline == 1 for deadline, _future in clock._sleepers)

    await wait_until(idle_timer_is_armed)
    assert (await repository.get_batch(created.batch.id)).status is BatchStatus.QUEUED
    clock.advance(1)
    await asyncio.wait_for(pipeline_entered.wait(), timeout=1)

    async def terminal() -> bool:
        return (await repository.get_batch(created.batch.id)).status is BatchStatus.COMPLETED

    await wait_until(terminal)
    assert flaky_repository.claim_attempts >= 2
    assert any(
        task.get_name() == "ocr-worker-0" and not task.done()
        for task in service._tasks
    )
    assert secret not in caplog.text
    await service.close()


def test_pipeline_failure_and_lifecycle_errors_discard_unsafe_causes() -> None:
    secret = "recognized OCR text /private/path.pdf"
    failure = PipelineFailure(
        PipelineErrorCode.INPUT_INVALID,
        retryable=False,
        cause=RuntimeError(secret),
    )
    lifecycle = ServiceLifecycleError(cause=RuntimeError(secret))

    assert secret not in str(failure)
    assert secret not in repr(failure)
    assert failure.__cause__ is None
    assert secret not in str(lifecycle)
    assert lifecycle.__cause__ is None

    with pytest.raises(Exception) as exc_info:
        OrchestrationService(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            OrchestrationSettings(),
            worker_identity="private/path worker",
        )
    assert secret not in str(exc_info.value)
