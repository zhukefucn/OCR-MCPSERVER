"""Bounded async facade over a single-owner synchronous OCR backend."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass
from enum import StrEnum
from queue import Empty, Full, Queue
import threading
from typing import Callable, Protocol

from ..domain import (
    ImageCandidate,
    SecondaryOCREngine,
    SecondaryOcrErrorCode,
    SecondaryOcrFailure,
    SecondaryOcrResult,
)
from ..services.observability import NullObservability, ObservabilitySink, best_effort


class SynchronousSecondaryOcrBackend(Protocol):
    """Backend whose complete lifecycle belongs to one worker thread."""

    def recognize(self, candidate: ImageCandidate) -> SecondaryOcrResult: ...

    def close(self) -> None: ...


class SecondaryOcrWorkerLifecycle(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass(slots=True)
class _Job:
    candidate: ImageCandidate
    outcome: Future[SecondaryOcrResult]


_STOP = object()


class SingleOwnerSecondaryOcrWorker:
    """Serialize synchronous inference on one bounded, dedicated owner thread."""

    engine = SecondaryOCREngine.PP_STRUCTURE_V3

    def __init__(
        self,
        backend_factory: Callable[[], SynchronousSecondaryOcrBackend],
        *,
        queue_capacity: int,
        observability: ObservabilitySink | None = None,
    ) -> None:
        if isinstance(queue_capacity, bool) or queue_capacity < 1:
            raise ValueError("queue capacity must be positive")
        self._factory = backend_factory
        self._observability = (
            observability if observability is not None else NullObservability()
        )
        self._queue: Queue[_Job | object] = Queue(maxsize=queue_capacity)
        self._lock = threading.RLock()
        self._lifecycle = SecondaryOcrWorkerLifecycle.CREATED
        self._thread: threading.Thread | None = None
        self._bootstrap: Future[None] | None = None
        self._done: Future[None] | None = None
        self._detached_outcomes: set[asyncio.Future[SecondaryOcrResult]] = set()
        self._waiting_jobs = 0

    @property
    def lifecycle(self) -> SecondaryOcrWorkerLifecycle:
        with self._lock:
            return self._lifecycle

    @property
    def queue_depth(self) -> int:
        with self._lock:
            return self._waiting_jobs

    def _observe_queue_depth(self) -> None:
        depth = self.queue_depth
        best_effort(lambda: self._observability.set_secondary_ocr_queue_depth(depth))

    @property
    def owner_thread_alive(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    async def start(self) -> None:
        with self._lock:
            if self._lifecycle is SecondaryOcrWorkerLifecycle.RUNNING:
                return
            if self._lifecycle is SecondaryOcrWorkerLifecycle.STARTING:
                bootstrap = self._bootstrap
            elif self._lifecycle is SecondaryOcrWorkerLifecycle.CREATED:
                self._lifecycle = SecondaryOcrWorkerLifecycle.STARTING
                self._bootstrap = Future()
                self._done = Future()
                bootstrap = self._bootstrap
                self._thread = threading.Thread(
                    target=self._run,
                    name="secondary-ocr-owner",
                    daemon=True,
                )
                self._thread.start()
            else:
                raise SecondaryOcrFailure(SecondaryOcrErrorCode.NOT_STARTED)
        assert bootstrap is not None
        start_failure: SecondaryOcrFailure | None = None
        try:
            await asyncio.shield(asyncio.wrap_future(bootstrap))
        except asyncio.CancelledError:
            await asyncio.shield(self.close())
            raise
        except SecondaryOcrFailure as exc:
            start_failure = exc
        if start_failure is not None:
            await self._join_owner_thread()
            raise start_failure

    async def recognize(self, candidate: ImageCandidate) -> SecondaryOcrResult:
        outcome: Future[SecondaryOcrResult] = Future()
        job = _Job(candidate=candidate, outcome=outcome)
        with self._lock:
            if self._lifecycle is not SecondaryOcrWorkerLifecycle.RUNNING:
                raise SecondaryOcrFailure(SecondaryOcrErrorCode.NOT_STARTED)
            saturated = False
            try:
                self._queue.put_nowait(job)
            except Full:
                saturated = True
            else:
                self._waiting_jobs += 1
            self._observe_queue_depth()
        if saturated:
            raise SecondaryOcrFailure(SecondaryOcrErrorCode.QUEUE_SATURATED)
        wrapped = asyncio.wrap_future(outcome)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            self._detached_outcomes.add(wrapped)
            wrapped.add_done_callback(self._consume_detached_outcome)
            self._observe_queue_depth()
            raise

    def _consume_detached_outcome(
        self, outcome: asyncio.Future[SecondaryOcrResult]
    ) -> None:
        self._detached_outcomes.discard(outcome)
        if not outcome.cancelled():
            try:
                outcome.exception()
            except BaseException:
                pass

    async def close(self) -> None:
        with self._lock:
            if self._lifecycle is SecondaryOcrWorkerLifecycle.CLOSED:
                self._observe_queue_depth()
                return
            if self._lifecycle is SecondaryOcrWorkerLifecycle.CREATED:
                self._lifecycle = SecondaryOcrWorkerLifecycle.CLOSED
                self._observe_queue_depth()
                return
            if self._lifecycle is SecondaryOcrWorkerLifecycle.CLOSING:
                done = self._done
            elif self._lifecycle is SecondaryOcrWorkerLifecycle.FAILED:
                self._lifecycle = SecondaryOcrWorkerLifecycle.CLOSED
                done = self._done
            else:
                self._lifecycle = SecondaryOcrWorkerLifecycle.CLOSING
                self._reject_queued_locked()
                self._queue.put_nowait(_STOP)
                done = self._done
        if done is not None:
            close_error: BaseException | None = None
            try:
                await asyncio.shield(asyncio.wrap_future(done))
            except BaseException as exc:
                close_error = exc
            await self._join_owner_thread()
            if close_error is not None:
                raise close_error

    async def _join_owner_thread(self) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            await asyncio.shield(asyncio.to_thread(thread.join))

    def _reject_queued_locked(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                return
            try:
                if isinstance(item, _Job):
                    self._waiting_jobs -= 1
                    _set_exception_if_pending(
                        item.outcome,
                        SecondaryOcrFailure(SecondaryOcrErrorCode.NOT_STARTED),
                    )
            finally:
                self._queue.task_done()
                self._observe_queue_depth()

    def _run(self) -> None:
        backend: SynchronousSecondaryOcrBackend | None = None
        close_failure: SecondaryOcrFailure | None = None
        try:
            try:
                backend = self._factory()
            except BaseException as exc:
                failure = SecondaryOcrFailure(
                    SecondaryOcrErrorCode.INITIALIZATION_UNAVAILABLE, cause=exc
                )
                with self._lock:
                    if self._lifecycle is not SecondaryOcrWorkerLifecycle.CLOSING:
                        self._lifecycle = SecondaryOcrWorkerLifecycle.FAILED
                    self._waiting_jobs = 0
                self._observe_queue_depth()
                assert self._bootstrap is not None
                _set_exception_if_pending(self._bootstrap, failure)
                return

            with self._lock:
                if self._lifecycle is SecondaryOcrWorkerLifecycle.CLOSING:
                    assert self._bootstrap is not None
                    _set_exception_if_pending(
                        self._bootstrap,
                        SecondaryOcrFailure(SecondaryOcrErrorCode.NOT_STARTED),
                    )
                else:
                    self._lifecycle = SecondaryOcrWorkerLifecycle.RUNNING
                    assert self._bootstrap is not None
                    _set_result_if_pending(self._bootstrap, None)

            while True:
                item = self._queue.get()
                if isinstance(item, _Job):
                    with self._lock:
                        self._waiting_jobs -= 1
                    self._observe_queue_depth()
                try:
                    if item is _STOP:
                        break
                    assert isinstance(item, _Job)
                    with self._lock:
                        accepting = (
                            self._lifecycle is SecondaryOcrWorkerLifecycle.RUNNING
                        )
                    if not accepting:
                        _set_exception_if_pending(
                            item.outcome,
                            SecondaryOcrFailure(SecondaryOcrErrorCode.NOT_STARTED),
                        )
                        continue
                    try:
                        result = backend.recognize(item.candidate)
                    except BaseException as exc:
                        _set_exception_if_pending(
                            item.outcome,
                            SecondaryOcrFailure(
                                SecondaryOcrErrorCode.INTERNAL_WORKER_FAILURE,
                                cause=exc,
                            ),
                        )
                    else:
                        _set_result_if_pending(item.outcome, result)
                finally:
                    self._queue.task_done()
        finally:
            if backend is not None:
                try:
                    backend.close()
                except BaseException as exc:
                    close_failure = SecondaryOcrFailure(
                        SecondaryOcrErrorCode.INTERNAL_WORKER_FAILURE, cause=exc
                    )
            with self._lock:
                if self._lifecycle is not SecondaryOcrWorkerLifecycle.FAILED:
                    self._lifecycle = SecondaryOcrWorkerLifecycle.CLOSED
                self._waiting_jobs = 0
            self._observe_queue_depth()
            if self._done is not None:
                if close_failure is None:
                    _set_result_if_pending(self._done, None)
                else:
                    _set_exception_if_pending(self._done, close_failure)


def _set_result_if_pending(future: Future, value: object) -> None:
    if not future.done():
        future.set_result(value)


def _set_exception_if_pending(future: Future, error: BaseException) -> None:
    if not future.done():
        future.set_exception(error)
