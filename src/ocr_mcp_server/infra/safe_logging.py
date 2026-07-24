"""Content-free structured event logging."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from queue import Empty, Full, Queue
import threading
import time
import weakref
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from ocr_mcp_server.domain.models import ProcessingStage


_MAX_COUNT = 1_000_000
_ERROR_CODES = frozenset(
    {
        "authentication_failed",
        "authentication_unavailable",
        "capacity_exceeded",
        "conflict",
        "internal_error",
        "invalid_request",
        "not_found",
        "orientation_uncertain",
        "service_unavailable",
        "unsupported_media_type",
    }
)


class SafeLogEventName(StrEnum):
    HTTP_REQUEST_COMPLETED = "http_request_completed"
    TASK_COMPLETED = "task_completed"
    PIPELINE_STAGE_COMPLETED = "pipeline_stage_completed"
    RECOVERY_COMPLETED = "recovery_completed"
    READINESS_CHECKED = "readiness_checked"
    OBSERVABILITY_FAILURE = "observability_failure"


def _invalid() -> ValueError:
    return ValueError("invalid log event")


def _canonical_id(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise _invalid()
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise _invalid() from None
    canonical = str(parsed)
    if canonical != value:
        raise _invalid()
    return canonical


def _count(value: object) -> bool:
    return (
        value is None
        or (
            type(value) is int
            and 0 <= value <= _MAX_COUNT
        )
    )


def _snapshot(event: object) -> SafeLogEvent:
    if type(event) is not SafeLogEvent:
        raise _invalid()
    return SafeLogEvent(
        event=object.__getattribute__(event, "event"),
        batch_id=object.__getattribute__(event, "batch_id"),
        file_id=object.__getattribute__(event, "file_id"),
        recovery_id=object.__getattribute__(event, "recovery_id"),
        stage=object.__getattribute__(event, "stage"),
        error_code=object.__getattribute__(event, "error_code"),
        duration_ms=object.__getattribute__(event, "duration_ms"),
        item_count=object.__getattribute__(event, "item_count"),
        success_count=object.__getattribute__(event, "success_count"),
        failure_count=object.__getattribute__(event, "failure_count"),
    )


@dataclass(frozen=True, slots=True)
class SafeLogEvent:
    """A finite event whose fields cannot carry arbitrary business content."""

    event: SafeLogEventName
    batch_id: str | None = None
    file_id: str | None = None
    recovery_id: str | None = None
    stage: ProcessingStage | None = None
    error_code: str | None = None
    duration_ms: float | None = None
    item_count: int | None = None
    success_count: int | None = None
    failure_count: int | None = None

    def __post_init__(self) -> None:
        if type(self.event) is not SafeLogEventName:
            raise _invalid()
        object.__setattr__(self, "event", SafeLogEventName(self.event.value))
        object.__setattr__(self, "batch_id", _canonical_id(self.batch_id))
        object.__setattr__(self, "file_id", _canonical_id(self.file_id))
        object.__setattr__(self, "recovery_id", _canonical_id(self.recovery_id))
        if self.stage is not None and type(self.stage) is not ProcessingStage:
            raise _invalid()
        if self.stage is not None:
            object.__setattr__(self, "stage", ProcessingStage(self.stage.value))
        if self.error_code is not None and (
            type(self.error_code) is not str
            or self.error_code not in _ERROR_CODES
        ):
            raise _invalid()
        if self.error_code is not None:
            object.__setattr__(self, "error_code", str(self.error_code))
        if self.duration_ms is not None and (
            type(self.duration_ms) not in {int, float}
            or not math.isfinite(self.duration_ms)
            or self.duration_ms < 0
        ):
            raise _invalid()
        if self.duration_ms is not None:
            object.__setattr__(self, "duration_ms", float(self.duration_ms))
        if not all(
            _count(value)
            for value in (self.item_count, self.success_count, self.failure_count)
        ):
            raise _invalid()
        for field_name in ("item_count", "success_count", "failure_count"):
            value = object.__getattribute__(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, int(value))


class JsonEventFormatter(logging.Formatter):
    """Format only the validated event attached by :class:`SafeEventLogger`."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            event = _snapshot(getattr(record, "safe_event", None))
        except Exception:
            event = SafeLogEvent(event=SafeLogEventName.OBSERVABILITY_FAILURE)
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname
            if record.levelname in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
            else "INFO",
        }
        for key, value in asdict(event).items():
            if value is None:
                continue
            payload[key] = (
                value.value
                if type(value) in {SafeLogEventName, ProcessingStage}
                else value
            )
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


class SafeEventLogger:
    """Emit validated events without allowing logging failures to escape."""

    def __init__(self, logger: logging.Logger) -> None:
        if not isinstance(logger, logging.Logger):
            raise ValueError("invalid logger")
        self._logger = logger

    def emit(self, event: SafeLogEvent) -> None:
        snapshot = _snapshot(event)
        try:
            record = self._logger.makeRecord(
                self._logger.name,
                logging.INFO,
                "",
                0,
                "",
                (),
                None,
            )
            record.safe_event = snapshot
            self._logger.handle(record)
        except Exception:
            return


class SafeEventSink(Protocol):
    def emit(self, event: SafeLogEvent) -> None: ...


class _SafeEventDispatchState:
    def __init__(
        self,
        sink: SafeEventSink,
        capacity: int,
        *,
        active: bool,
    ) -> None:
        try:
            self.sink_reference = weakref.ref(sink)
            self.sink_strong = None
        except TypeError:
            self.sink_reference = None
            self.sink_strong = sink
        self.queue: Queue[SafeLogEvent | object] = Queue(maxsize=capacity)
        self.lock = threading.Lock()
        self.active = active
        self.started = False
        self.closed = False
        self.dropped = 0
        self.stop_token = object()


def _stop_safe_event_state(state: _SafeEventDispatchState) -> None:
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


def _run_safe_event_state(state: _SafeEventDispatchState) -> None:
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
            if type(item) is not SafeLogEvent:
                continue
            with state.lock:
                closed = state.closed
            if closed:
                continue
            sink = (
                state.sink_strong
                if state.sink_reference is None
                else state.sink_reference()
            )
            if sink is not None:
                try:
                    sink.emit(item)
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        finally:
            state.queue.task_done()


class SafeEventLogDispatcher:
    """Bounded, content-safe logging adapter with one FIFO worker."""

    def __init__(
        self,
        sink: SafeEventSink,
        *,
        capacity: int = 256,
        autostart: bool = True,
    ) -> None:
        try:
            valid_sink = callable(getattr(sink, "emit"))
        except Exception:
            valid_sink = False
        if (
            not valid_sink
            or isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or capacity < 1
            or not isinstance(autostart, bool)
        ):
            raise _invalid()
        self._state = _SafeEventDispatchState(
            sink, capacity, active=autostart
        )
        self._thread = threading.Thread(
            target=_run_safe_event_state,
            args=(self._state,),
            name=f"ocr-safe-log-{id(self._state):x}",
            daemon=True,
        )
        self._finalizer = weakref.finalize(
            self, _stop_safe_event_state, self._state
        )

    @property
    def pending(self) -> int:
        return self._state.queue.qsize()

    @property
    def dropped(self) -> int:
        with self._state.lock:
            return self._state.dropped

    @property
    def worker_count(self) -> int:
        return 1

    @property
    def alive_workers(self) -> int:
        return int(self._thread.is_alive())

    def emit(self, event: SafeLogEvent) -> None:
        snapshot = _snapshot(event)
        with self._state.lock:
            if self._state.closed:
                self._state.dropped += 1
                return
            try:
                self._state.queue.put_nowait(snapshot)
            except Full:
                self._state.dropped += 1
                return
            if self._state.active and not self._state.started:
                self._start_locked()

    def activate(self) -> None:
        with self._state.lock:
            if self._state.closed or self._state.active:
                return
            self._state.active = True
            if self._state.queue.qsize() and not self._state.started:
                self._start_locked()

    def _start_locked(self) -> None:
        try:
            self._thread.start()
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
        timeout = _timeout(timeout)
        deadline = time.monotonic() + timeout
        while self._state.queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.001)
        return True

    def close(self, *, timeout: float = 0.1) -> None:
        timeout = _timeout(timeout)
        deadline = time.monotonic() + timeout
        self.drain(timeout)
        self._finalizer()
        if self._thread.ident is not None:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                self._thread.join(remaining)

    def wait_closed(self, timeout: float = 1.0) -> bool:
        timeout = _timeout(timeout)
        if self._thread.ident is not None:
            self._thread.join(timeout)
        return not self._thread.is_alive()


def _timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise _invalid()
    return float(value)


def nonblocking_safe_event_logger(
    sink: SafeEventSink,
    *,
    autostart: bool = True,
) -> tuple[SafeEventSink, SafeEventLogDispatcher | None]:
    if isinstance(sink, SafeEventLogDispatcher):
        return sink, None
    dispatcher = SafeEventLogDispatcher(sink, autostart=autostart)
    return dispatcher, dispatcher


__all__ = [
    "JsonEventFormatter",
    "SafeEventLogDispatcher",
    "SafeEventLogger",
    "SafeEventSink",
    "SafeLogEvent",
    "SafeLogEventName",
    "nonblocking_safe_event_logger",
]
