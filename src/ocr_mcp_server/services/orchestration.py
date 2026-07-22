"""Durable, transport-independent scheduling for claimed file pipelines."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from ..domain.errors import DomainError, InputValidationError, LeaseConflictError
from ..domain.models import FileStatus, ProcessingStage, utc_now
from ..domain.progress import ProgressCounters, ProgressUnit
from ..domain.tasks import FileTaskSnapshot, LeaseClaim
from ..infra.task_repository import TaskRepository
from ..settings import OrchestrationSettings


class PipelineErrorCode(StrEnum):
    DEPENDENCY_UNAVAILABLE = "pipeline_dependency_unavailable"
    INPUT_INVALID = "pipeline_input_invalid"
    PROCESSING_FAILED = "pipeline_processing_failed"
    UNEXPECTED = "pipeline_unexpected"


_PIPELINE_ERROR_MESSAGES = {
    PipelineErrorCode.DEPENDENCY_UNAVAILABLE: "A pipeline dependency is unavailable.",
    PipelineErrorCode.INPUT_INVALID: "The pipeline input is invalid.",
    PipelineErrorCode.PROCESSING_FAILED: "The file pipeline failed safely.",
    PipelineErrorCode.UNEXPECTED: "The file pipeline failed unexpectedly.",
}


class PipelineFailure(DomainError):
    """Typed pipeline failure that retains only a stable code and retry policy."""

    def __init__(
        self,
        code: PipelineErrorCode,
        *,
        retryable: bool,
        cause: BaseException | None = None,
    ) -> None:
        del cause
        if not isinstance(code, PipelineErrorCode) or not isinstance(retryable, bool):
            raise InputValidationError()
        self.code = code.value
        self.safe_message = _PIPELINE_ERROR_MESSAGES[code]
        self.retryable = retryable
        Exception.__init__(self, self.safe_message)


class ServiceLifecycleError(DomainError):
    code = "orchestration_lifecycle_invalid"
    safe_message = "The orchestration service lifecycle operation is invalid."


class PipelineCancellation:
    """Cooperative cancellation context; service shutdown is not customer cancel."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()

    def checkpoint(self) -> None:
        if self._event.is_set():
            raise asyncio.CancelledError()

    def _cancel(self) -> None:
        self._event.set()


@dataclass(frozen=True, slots=True)
class PipelineFileIdentity:
    file_id: str
    batch_id: str
    position: int
    attempt_count: int


@dataclass(frozen=True, slots=True)
class PipelineResult:
    with_warnings: bool = False

    @classmethod
    def success(cls) -> PipelineResult:
        return cls(False)

    @classmethod
    def success_with_warnings(cls) -> PipelineResult:
        return cls(True)


class ProgressReporter(Protocol):
    async def report(
        self,
        stage: ProcessingStage,
        counters: ProgressCounters | None = None,
    ) -> FileTaskSnapshot: ...


class FilePipeline(Protocol):
    async def run(
        self,
        file: PipelineFileIdentity,
        progress: ProgressReporter,
        cancellation: PipelineCancellation,
    ) -> PipelineResult: ...


@dataclass(frozen=True, slots=True)
class ProgressNotification:
    batch_id: str
    file_id: str
    status: FileStatus
    stage: ProcessingStage
    progress: int
    completed_units: int | None
    total_units: int | None
    progress_unit: ProgressUnit | None
    attempt_count: int
    error_code: str | None
    version: int
    timestamp: datetime


class ProgressNotificationSink(Protocol):
    async def publish(self, notification: ProgressNotification) -> None: ...


class OrchestrationClock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class _SystemClock:
    def now(self) -> datetime:
        return utc_now()

    def monotonic(self) -> float:
        return asyncio.get_running_loop().time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class _NotificationState:
    status: FileStatus
    stage: ProcessingStage
    progress: int
    completed_units: int | None
    total_units: int | None
    progress_unit: ProgressUnit | None
    version: int
    sent_at: float


_TERMINAL_STATUSES = frozenset(
    {
        FileStatus.COMPLETED,
        FileStatus.COMPLETED_WITH_WARNINGS,
        FileStatus.FAILED,
        FileStatus.CANCELLED,
    }
)
_REPORTABLE_STAGES = frozenset(
    {
        ProcessingStage.MINERU_PARSING,
        ProcessingStage.COLLECTING_IMAGES,
        ProcessingStage.DETECTING_ORIENTATION,
        ProcessingStage.CLASSIFYING_IMAGES,
        ProcessingStage.RECOGNIZING_IMAGES,
        ProcessingStage.MERGING,
        ProcessingStage.PACKAGING,
        ProcessingStage.PUBLISHING,
    }
)


class _NotificationDispatcher:
    def __init__(
        self,
        sink: ProgressNotificationSink | None,
        clock: OrchestrationClock,
        minimum_interval: float,
        maximum_tracked: int,
    ) -> None:
        self._sink = sink
        self._clock = clock
        self._minimum_interval = minimum_interval
        self._maximum_tracked = maximum_tracked
        self._states: dict[str, _NotificationState] = {}

    @property
    def tracking_size(self) -> int:
        return len(self._states)

    async def emit(self, snapshot: FileTaskSnapshot) -> None:
        if self._sink is None:
            return
        previous = self._states.get(snapshot.id)
        now = self._clock.monotonic()
        counters_changed = previous is None or (
            snapshot.completed_units,
            snapshot.total_units,
            snapshot.progress_unit,
        ) != (
            previous.completed_units,
            previous.total_units,
            previous.progress_unit,
        )
        immediate = (
            previous is None
            or snapshot.stage is not previous.stage
            or snapshot.status is not previous.status
            or snapshot.status in _TERMINAL_STATUSES
        )
        material = (
            previous is None
            or snapshot.progress > previous.progress
            or counters_changed
        )
        if previous is not None and (
            snapshot.version <= previous.version
            or snapshot.progress < previous.progress
            or not material
            or (not immediate and now - previous.sent_at < self._minimum_interval)
        ):
            return
        state = _NotificationState(
            snapshot.status,
            snapshot.stage,
            snapshot.progress,
            snapshot.completed_units,
            snapshot.total_units,
            snapshot.progress_unit,
            snapshot.version,
            now,
        )
        self._states[snapshot.id] = state
        while len(self._states) > self._maximum_tracked:
            self._states.pop(next(iter(self._states)))
        notification = ProgressNotification(
            batch_id=snapshot.batch_id,
            file_id=snapshot.id,
            status=snapshot.status,
            stage=snapshot.stage,
            progress=snapshot.progress,
            completed_units=snapshot.completed_units,
            total_units=snapshot.total_units,
            progress_unit=snapshot.progress_unit,
            attempt_count=snapshot.attempt_count,
            error_code=snapshot.last_error_code,
            version=snapshot.version,
            timestamp=snapshot.updated_at,
        )
        try:
            await self._sink.publish(notification)
        except BaseException:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
        finally:
            if snapshot.status in _TERMINAL_STATUSES:
                self._states.pop(snapshot.id, None)


class _LeaseProgressReporter:
    def __init__(
        self,
        repository: TaskRepository,
        claim: LeaseClaim,
        cancellation: PipelineCancellation,
        notifier: _NotificationDispatcher,
        clock: OrchestrationClock,
    ) -> None:
        self._repository = repository
        self._claim = claim
        self._cancellation = cancellation
        self._notifier = notifier
        self._clock = clock

    async def report(
        self,
        stage: ProcessingStage,
        counters: ProgressCounters | None = None,
    ) -> FileTaskSnapshot:
        self._cancellation.checkpoint()
        if not isinstance(stage, ProcessingStage) or stage not in _REPORTABLE_STAGES:
            raise InputValidationError()
        try:
            snapshot = await self._repository.update_progress(
                self._claim.file.id,
                self._claim.lease_token,
                stage=stage,
                counters=counters,
                now=self._clock.now(),
            )
        except LeaseConflictError:
            self._cancellation._cancel()
            raise
        await self._notifier.emit(snapshot)
        self._cancellation.checkpoint()
        return snapshot


class OrchestrationService:
    """Bounded local scheduler whose durable source of truth is the repository."""

    def __init__(
        self,
        repository: TaskRepository,
        pipeline: FilePipeline,
        settings: OrchestrationSettings,
        *,
        notification_sink: ProgressNotificationSink | None = None,
        clock: OrchestrationClock | None = None,
        worker_identity: str,
    ) -> None:
        if (
            not isinstance(worker_identity, str)
            or not worker_identity.strip()
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", worker_identity)
            is None
        ):
            raise InputValidationError()
        self._repository = repository
        self._pipeline = pipeline
        self._settings = settings
        self._clock = clock or _SystemClock()
        self._worker_identity = worker_identity
        self._wake_queue: asyncio.Queue[None] = asyncio.Queue(
            maxsize=settings.wake_queue_capacity
        )
        self._notifier = _NotificationDispatcher(
            notification_sink,
            self._clock,
            settings.notification_min_interval_seconds,
            settings.worker_count,
        )
        self._tasks: set[asyncio.Task[None]] = set()
        self._active_file_ids: set[str] = set()
        self._started = False
        self._closing = False
        self._closed = False

    @property
    def notification_tracking_size(self) -> int:
        return self._notifier.tracking_size

    async def start(self) -> None:
        if self._started or self._closed:
            raise ServiceLifecycleError()
        await self._repository.recover_expired_leases(now=self._clock.now())
        self._started = True
        for number in range(self._settings.worker_count):
            self._create_task(self._worker(number), f"ocr-worker-{number}")
        self._create_task(self._idle_poll_loop(), "ocr-durable-poll")
        self._create_task(self._recovery_loop(), "ocr-lease-recovery")
        self.notify_work()

    def notify_work(self) -> bool:
        if self._closed or self._closing:
            return False
        try:
            self._wake_queue.put_nowait(None)
        except asyncio.QueueFull:
            return False
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closing = True
        self._closed = True
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            done, pending = await asyncio.wait(tuple(self._tasks), timeout=1.0)
            for task in done:
                self._consume_task_result(task)
            for task in pending:
                task.add_done_callback(self._consume_task_result)
        self._tasks.clear()
        self._active_file_ids.clear()

    def _create_task(self, coroutine, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)

    def _task_finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        self._consume_task_result(task)

    @staticmethod
    def _consume_task_result(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            return

    async def _worker(self, number: int) -> None:
        worker_id = f"{self._worker_identity}:{number}"
        while not self._closing:
            await self._wake_queue.get()
            try:
                while not self._closing:
                    claim = await self._repository.claim_next(
                        worker_id,
                        now=self._clock.now(),
                        lease_seconds=self._settings.lease_seconds,
                    )
                    if claim is None:
                        break
                    self.notify_work()
                    await self._execute_claim(claim)
            finally:
                self._wake_queue.task_done()

    async def _execute_claim(self, claim: LeaseClaim) -> None:
        cancellation = PipelineCancellation()
        await self._notifier.emit(claim.file)
        heartbeat: asyncio.Task[None] | None = None
        pipeline_task: asyncio.Task[PipelineResult] | None = None
        while claim.file.id in self._active_file_ids and not self._closing:
            await asyncio.sleep(0)
        if self._closing:
            return
        self._active_file_ids.add(claim.file.id)
        try:
            reporter = _LeaseProgressReporter(
                self._repository,
                claim,
                cancellation,
                self._notifier,
                self._clock,
            )
            identity = PipelineFileIdentity(
                claim.file.id,
                claim.file.batch_id,
                claim.file.position,
                claim.file.attempt_count,
            )
            pipeline_task = asyncio.create_task(
                self._pipeline.run(identity, reporter, cancellation)
            )
            heartbeat = asyncio.create_task(
                self._heartbeat(claim, cancellation, pipeline_task)
            )
            try:
                result = await pipeline_task
                if not isinstance(result, PipelineResult):
                    raise TypeError()
                snapshot = await self._repository.complete_file(
                    claim.file.id,
                    claim.lease_token,
                    with_warnings=result.with_warnings,
                    now=self._clock.now(),
                )
                await self._notifier.emit(snapshot)
            except PipelineFailure as failure:
                if failure.retryable:
                    snapshot = await self._repository.retry_or_fail(
                        claim.file.id,
                        claim.lease_token,
                        error_code=failure.code,
                        now=self._clock.now(),
                    )
                else:
                    snapshot = await self._repository.fail_file(
                        claim.file.id,
                        claim.lease_token,
                        error_code=failure.code,
                        now=self._clock.now(),
                    )
                await self._notifier.emit(snapshot)
                if snapshot.status is FileStatus.QUEUED:
                    self.notify_work()
            except LeaseConflictError:
                cancellation._cancel()
            except asyncio.CancelledError:
                cancellation._cancel()
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except Exception:
                try:
                    snapshot = await self._repository.fail_file(
                        claim.file.id,
                        claim.lease_token,
                        error_code=PipelineErrorCode.UNEXPECTED.value,
                        now=self._clock.now(),
                    )
                except LeaseConflictError:
                    cancellation._cancel()
                else:
                    await self._notifier.emit(snapshot)
        finally:
            cancellation._cancel()
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if pipeline_task is not None and not pipeline_task.done():
                pipeline_task.cancel()
            self._active_file_ids.discard(claim.file.id)

    async def _heartbeat(
        self,
        claim: LeaseClaim,
        cancellation: PipelineCancellation,
        pipeline_task: asyncio.Task[PipelineResult],
    ) -> None:
        while not self._closing and not pipeline_task.done():
            await self._clock.sleep(self._settings.heartbeat_seconds)
            if self._closing or pipeline_task.done():
                return
            try:
                await self._repository.heartbeat(
                    claim.file.id,
                    claim.lease_token,
                    now=self._clock.now(),
                    lease_seconds=self._settings.lease_seconds,
                )
            except Exception:
                cancellation._cancel()
                pipeline_task.cancel()
                return

    async def _idle_poll_loop(self) -> None:
        while not self._closing:
            await self._clock.sleep(self._settings.idle_poll_seconds)
            if not self._closing:
                self.notify_work()

    async def _recovery_loop(self) -> None:
        while not self._closing:
            await self._clock.sleep(self._settings.recovery_scan_seconds)
            if self._closing:
                return
            recovered = await self._repository.recover_expired_leases(
                now=self._clock.now()
            )
            if recovered:
                self.notify_work()
