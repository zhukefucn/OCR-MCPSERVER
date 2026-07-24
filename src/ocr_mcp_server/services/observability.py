"""Finite, transport-neutral observability contracts."""

from __future__ import annotations

import asyncio
import math
from queue import Empty, Full, Queue
import re
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import InitVar, dataclass
from enum import StrEnum
from typing import Protocol

from ocr_mcp_server.domain.models import ProcessingStage


_HTTP_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)
_STATUS_CLASSES = frozenset({"2xx", "3xx", "4xx", "5xx"})
_METHOD_TOKEN = re.compile(r"[A-Za-z]+\Z")
_ROUTE_TEMPLATE = re.compile(
    r"/(?:[A-Za-z][A-Za-z0-9_-]*|\{[A-Za-z][A-Za-z0-9_]*\})"
    r"(?:/(?:[A-Za-z][A-Za-z0-9_-]*|\{[A-Za-z][A-Za-z0-9_]*\}))*\Z"
)


def _invalid() -> ValueError:
    return ValueError("invalid observation")


def _duration(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise _invalid()
    return float(value)


def _queue_depth(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid()
    return value


class TaskOutcome(StrEnum):
    COMPLETED = "completed"
    WARNING = "warning"
    RETRY = "retry"
    FAILED = "failed"
    UNEXPECTED_FAILURE = "unexpected_failure"
    LEASE_CONFLICT = "lease_conflict"
    CANCELLED = "cancelled"


class StageOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RecoveryOutcome(StrEnum):
    COMPLETED = "completed"
    UNCERTAIN = "uncertain"
    CONFLICT = "conflict"
    INVALID_TOKEN = "invalid_token"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class DependencyName(StrEnum):
    SQLITE = "sqlite"
    MINERU = "mineru"
    PADDLE = "paddle"


@dataclass(frozen=True, slots=True)
class HttpObservation:
    method: str
    route: str
    status_class: str
    duration_seconds: float
    route_allowlist: InitVar[frozenset[str]] = frozenset()

    def __post_init__(self, route_allowlist: frozenset[str]) -> None:
        if (
            not isinstance(self.method, str)
            or _METHOD_TOKEN.fullmatch(self.method) is None
            or not isinstance(self.route, str)
            or not isinstance(self.status_class, str)
            or self.status_class not in _STATUS_CLASSES
            or type(route_allowlist) is not frozenset
            or any(
                not isinstance(route, str)
                or route == "unmatched"
                or _ROUTE_TEMPLATE.fullmatch(route) is None
                for route in route_allowlist
            )
        ):
            raise _invalid()
        normalized_method = self.method.upper()
        object.__setattr__(
            self,
            "method",
            normalized_method if normalized_method in _HTTP_METHODS else "OTHER",
        )
        object.__setattr__(
            self,
            "route",
            self.route if self.route in route_allowlist else "unmatched",
        )
        object.__setattr__(
            self, "duration_seconds", _duration(self.duration_seconds)
        )


class ObservabilitySink(Protocol):
    def observe_http(self, observation: HttpObservation) -> None: ...

    def observe_task(
        self, outcome: TaskOutcome, duration_seconds: float
    ) -> None: ...

    def observe_stage(
        self,
        stage: ProcessingStage,
        outcome: StageOutcome,
        duration_seconds: float,
    ) -> None: ...

    def observe_recovery(self, outcome: RecoveryOutcome) -> None: ...

    def set_orchestration_queue_depth(self, depth: int) -> None: ...

    def set_secondary_ocr_queue_depth(self, depth: int) -> None: ...

    def set_dependency_ready(
        self, dependency: DependencyName, ready: bool
    ) -> None: ...


class NullObservability:
    """Validating no-op sink used when observations are disabled."""

    def observe_http(self, observation: HttpObservation) -> None:
        if not isinstance(observation, HttpObservation):
            raise _invalid()

    def observe_task(self, outcome: TaskOutcome, duration_seconds: float) -> None:
        if not isinstance(outcome, TaskOutcome):
            raise _invalid()
        _duration(duration_seconds)

    def observe_stage(
        self,
        stage: ProcessingStage,
        outcome: StageOutcome,
        duration_seconds: float,
    ) -> None:
        if not isinstance(stage, ProcessingStage) or not isinstance(
            outcome, StageOutcome
        ):
            raise _invalid()
        _duration(duration_seconds)

    def observe_recovery(self, outcome: RecoveryOutcome) -> None:
        if not isinstance(outcome, RecoveryOutcome):
            raise _invalid()

    def set_orchestration_queue_depth(self, depth: int) -> None:
        _queue_depth(depth)

    def set_secondary_ocr_queue_depth(self, depth: int) -> None:
        _queue_depth(depth)

    def set_dependency_ready(self, dependency: DependencyName, ready: bool) -> None:
        if not isinstance(dependency, DependencyName) or not isinstance(ready, bool):
            raise _invalid()


def best_effort(observation: Callable[[], None]) -> None:
    """Run an observation without exposing or propagating ordinary failures."""

    if not callable(observation):
        raise _invalid()
    try:
        observation()
    except asyncio.CancelledError:
        return
    except Exception:
        return


class _ObservationDispatchState:
    """Worker-owned state that deliberately has no dispatcher back-reference."""

    def __init__(
        self, sink: ObservabilitySink, capacity: int, *, active: bool
    ) -> None:
        try:
            self.sink_reference = weakref.ref(sink)
            self.sink_strong = None
        except TypeError:
            self.sink_reference = None
            self.sink_strong = sink
        self.queue: Queue[tuple[str, tuple[object, ...]] | object] = Queue(
            maxsize=capacity
        )
        self.lock = threading.Lock()
        self.closed = False
        self.active = active
        self.started = False
        self.dropped = 0
        self.stop_token = object()


def _stop_dispatch_state(state: _ObservationDispatchState) -> None:
    """Stop exactly one dispatch state and discard its bounded queued payloads."""

    with state.lock:
        if state.closed:
            return
        state.closed = True
        started = state.started
    while True:
        try:
            state.queue.get_nowait()
        except Empty:
            break
        else:
            state.queue.task_done()
    if started:
        try:
            state.queue.put_nowait(state.stop_token)
        except Full:
            return


def _run_dispatch_state(state: _ObservationDispatchState) -> None:
    """Run without retaining the public dispatcher owner."""

    while True:
        try:
            item = state.queue.get(timeout=0.05)
        except Empty:
            with state.lock:
                if state.closed:
                    return
            continue
        try:
            if item is state.stop_token:
                return
            with state.lock:
                closed = state.closed
            if closed:
                continue
            method, args = item
            sink = (
                state.sink_strong
                if state.sink_reference is None
                else state.sink_reference()
            )
            if sink is not None:
                best_effort(lambda: getattr(sink, method)(*args))
        finally:
            state.queue.task_done()


class ObservationDispatcher(NullObservability):
    """Bounded asynchronous adapter with exactly one FIFO sink worker."""

    def __init__(
        self,
        sink: ObservabilitySink,
        *,
        capacity: int = 256,
        worker_count: int = 1,
        autostart: bool = True,
    ) -> None:
        if (
            sink is self
            or isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or capacity < 1
            or isinstance(worker_count, bool)
            or not isinstance(worker_count, int)
            or worker_count != 1
            or not isinstance(autostart, bool)
        ):
            raise _invalid()
        self._state = _ObservationDispatchState(
            sink, capacity, active=autostart
        )
        self._threads = (
            threading.Thread(
                target=_run_dispatch_state,
                args=(self._state,),
                name=f"ocr-observation-{id(self._state):x}",
                daemon=True,
            ),
        )
        self._finalizer = weakref.finalize(self, _stop_dispatch_state, self._state)

    @property
    def pending(self) -> int:
        return self._state.queue.qsize()

    @property
    def dropped(self) -> int:
        with self._state.lock:
            return self._state.dropped

    @property
    def worker_count(self) -> int:
        return len(self._threads)

    @property
    def alive_workers(self) -> int:
        return sum(thread.is_alive() for thread in self._threads)

    def observe_http(self, observation: HttpObservation) -> None:
        super().observe_http(observation)
        self._submit("observe_http", observation)

    def observe_task(self, outcome: TaskOutcome, duration_seconds: float) -> None:
        super().observe_task(outcome, duration_seconds)
        self._submit("observe_task", outcome, float(duration_seconds))

    def observe_stage(
        self,
        stage: ProcessingStage,
        outcome: StageOutcome,
        duration_seconds: float,
    ) -> None:
        super().observe_stage(stage, outcome, duration_seconds)
        self._submit("observe_stage", stage, outcome, float(duration_seconds))

    def observe_recovery(self, outcome: RecoveryOutcome) -> None:
        super().observe_recovery(outcome)
        self._submit("observe_recovery", outcome)

    def set_orchestration_queue_depth(self, depth: int) -> None:
        super().set_orchestration_queue_depth(depth)
        self._submit("set_orchestration_queue_depth", depth)

    def set_secondary_ocr_queue_depth(self, depth: int) -> None:
        super().set_secondary_ocr_queue_depth(depth)
        self._submit("set_secondary_ocr_queue_depth", depth)

    def set_dependency_ready(self, dependency: DependencyName, ready: bool) -> None:
        super().set_dependency_ready(dependency, ready)
        self._submit("set_dependency_ready", dependency, ready)

    def activate(self) -> None:
        """Allow queued observations to start the single FIFO worker."""

        with self._state.lock:
            if self._state.closed or self._state.active:
                return
            self._state.active = True
            if self._state.queue.qsize() and not self._state.started:
                self._start_locked()

    def _submit(self, method: str, *args: object) -> None:
        with self._state.lock:
            if self._state.closed:
                self._state.dropped += 1
                return
            try:
                self._state.queue.put_nowait((method, args))
            except Full:
                self._state.dropped += 1
                return
            if self._state.active and not self._state.started:
                self._start_locked()

    def _start_locked(self) -> None:
        try:
            self._threads[0].start()
        except Exception:
            self._state.closed = True
            while True:
                try:
                    self._state.queue.get_nowait()
                except Empty:
                    break
                else:
                    self._state.dropped += 1
                    self._state.queue.task_done()
            return
        self._state.started = True

    def drain(self, timeout: float = 1.0) -> bool:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout < 0
        ):
            raise _invalid()
        deadline = time.monotonic() + float(timeout)
        while self._state.queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.001)
        return True

    def close(self, *, timeout: float = 0.1) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout < 0
        ):
            raise _invalid()
        timeout = float(timeout)
        deadline = time.monotonic() + timeout
        self.drain(timeout)
        self._finalizer()
        for thread in self._threads:
            if thread.ident is None:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

    def wait_closed(self, timeout: float = 1.0) -> bool:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout < 0
        ):
            raise _invalid()
        deadline = time.monotonic() + float(timeout)
        for thread in self._threads:
            if thread.ident is None:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
        return self.alive_workers == 0


def nonblocking_observability(
    sink: ObservabilitySink | None,
    *,
    autostart: bool = True,
) -> tuple[ObservabilitySink, ObservationDispatcher | None]:
    """Return a safe call-site sink and an owned dispatcher, if one was created."""

    resolved: ObservabilitySink = sink if sink is not None else NullObservability()
    if isinstance(resolved, ObservationDispatcher) or type(resolved) is NullObservability:
        return resolved, None
    dispatcher = ObservationDispatcher(resolved, autostart=autostart)
    return dispatcher, dispatcher


__all__ = [
    "DependencyName",
    "HttpObservation",
    "NullObservability",
    "ObservationDispatcher",
    "ObservabilitySink",
    "RecoveryOutcome",
    "StageOutcome",
    "TaskOutcome",
    "best_effort",
    "nonblocking_observability",
]
