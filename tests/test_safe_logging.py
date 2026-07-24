from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import threading
import time
from uuid import uuid4

import pytest

from ocr_mcp_server.domain.models import ProcessingStage
from ocr_mcp_server.infra.safe_logging import (
    JsonEventFormatter,
    SafeEventLogDispatcher,
    SafeEventLogger,
    SafeLogEvent,
    SafeLogEventName,
)


def test_safe_log_event_is_frozen_slotted_and_accepts_only_bounded_fields() -> None:
    event = SafeLogEvent(
        event=SafeLogEventName.HTTP_REQUEST_COMPLETED,
        batch_id=str(uuid4()),
        file_id=str(uuid4()),
        recovery_id=str(uuid4()),
        stage=ProcessingStage.MERGING,
        error_code="service_unavailable",
        duration_ms=12.5,
        item_count=3,
        success_count=2,
        failure_count=1,
    )

    assert not hasattr(event, "__dict__")
    with pytest.raises((AttributeError, TypeError)):
        event.item_count = 4  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "canary"),
    [
        ("event", "recognized customer OCR text"),
        ("event", "private-invoice.pdf"),
        ("event", "https://files.example.test/private.pdf"),
        ("batch_id", r"C:\customers\private.pdf"),
        ("file_id", "/srv/ocr/private.pdf"),
        ("recovery_id", "opaque-recovery-token-secret"),
        ("error_code", "api-key-secret-value"),
        ("error_code", "Authorization: Bearer raw-token"),
        ("error_code", "exception says private filename"),
    ],
)
def test_safe_log_event_rejects_sensitive_free_text_content_free(
    field: str, canary: str
) -> None:
    arguments: dict[str, object] = {
        "event": SafeLogEventName.HTTP_REQUEST_COMPLETED,
    }
    arguments[field] = canary

    with pytest.raises(ValueError) as exc_info:
        SafeLogEvent(**arguments)  # type: ignore[arg-type]
    assert str(exc_info.value) == "invalid log event"
    assert canary not in str(exc_info.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stage", "merging"),
        ("duration_ms", True),
        ("duration_ms", -1),
        ("duration_ms", math.inf),
        ("duration_ms", math.nan),
        ("item_count", True),
        ("item_count", -1),
        ("item_count", 1.5),
        ("item_count", 1_000_001),
        ("success_count", RuntimeError("private exception")),
    ],
)
def test_safe_log_event_rejects_invalid_types_and_unbounded_numbers(
    field: str, value: object
) -> None:
    arguments: dict[str, object] = {
        "event": SafeLogEventName.HTTP_REQUEST_COMPLETED,
    }
    arguments[field] = value
    with pytest.raises(ValueError, match="^invalid log event$"):
        SafeLogEvent(**arguments)  # type: ignore[arg-type]


def test_json_formatter_emits_one_line_with_only_approved_keys() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonEventFormatter())
    underlying = logging.Logger("safe-test", level=logging.INFO)
    underlying.addHandler(handler)
    logger = SafeEventLogger(underlying)
    event = SafeLogEvent(
        event=SafeLogEventName.HTTP_REQUEST_COMPLETED,
        duration_ms=7.25,
        item_count=2,
    )

    logger.emit(event)

    rendered = stream.getvalue()
    assert rendered.count("\n") == 1
    payload = json.loads(rendered)
    assert set(payload) == {
        "timestamp",
        "level",
        "event",
        "duration_ms",
        "item_count",
    }
    assert payload["level"] == "INFO"
    assert payload["event"] == "http_request_completed"
    assert payload["timestamp"].endswith("Z")


def test_formatter_never_serializes_record_message_args_exception_or_extras() -> None:
    canaries = [
        "recognized customer OCR text",
        "private-invoice.pdf",
        "https://files.example.test/private.pdf",
        r"C:\customers\private.pdf",
        "/srv/ocr/private.pdf",
        "api-key-secret-value",
        "Authorization: Bearer raw-token",
        "opaque-recovery-token-secret",
        "exception says private filename",
    ]
    record = logging.LogRecord(
        "unsafe",
        logging.ERROR,
        __file__,
        1,
        canaries[0],
        tuple(canaries[1:]),
        (RuntimeError, RuntimeError(canaries[-1]), None),
    )
    record.stack_info = canaries[-2]
    record.arbitrary_extra = canaries[3]
    record.safe_event = SafeLogEvent(event=SafeLogEventName.HTTP_REQUEST_COMPLETED)

    rendered = JsonEventFormatter().format(record)

    assert json.loads(rendered)["event"] == "http_request_completed"
    assert all(canary not in rendered for canary in canaries)


def test_subclass_cannot_smuggle_overridden_fields_into_formatter_or_logger() -> None:
    canary = "TOP_SECRET_CANARY"

    class SmuggledEvent(SafeLogEvent):
        armed = False

        def __getattribute__(self, name: str) -> object:
            if name == "error_code" and type(self).armed:
                return canary
            return super().__getattribute__(name)

    event = SmuggledEvent(event=SafeLogEventName.HTTP_REQUEST_COMPLETED)
    SmuggledEvent.armed = True
    record = logging.LogRecord(
        "safe-subclass-test", logging.INFO, __file__, 1, "", (), None
    )
    record.safe_event = event

    rendered = JsonEventFormatter().format(record)

    assert json.loads(rendered)["event"] == "observability_failure"
    assert canary not in rendered

    records: list[logging.LogRecord] = []

    class RecordingHandler(logging.Handler):
        def emit(self, emitted: logging.LogRecord) -> None:
            records.append(emitted)

    underlying = logging.Logger("safe-subclass-emit-test", level=logging.INFO)
    underlying.addHandler(RecordingHandler())
    with pytest.raises(ValueError) as exc_info:
        SafeEventLogger(underlying).emit(event)
    assert str(exc_info.value) == "invalid log event"
    assert canary not in str(exc_info.value)
    assert records == []


def test_string_subclass_cannot_smuggle_caller_content_through_uuid_fields() -> None:
    canary = "TOP_SECRET_SCALAR_CANARY"
    canonical = "12345678-1234-5678-9234-567812345678"

    class SmuggledString(str):
        def replace(self, old: str, new: str, count: int = -1) -> str:
            del old, new, count
            return canonical

        def __eq__(self, other: object) -> bool:
            return other == canonical

        __hash__ = str.__hash__

    smuggled = SmuggledString(canary)
    event = SafeLogEvent(event=SafeLogEventName.HTTP_REQUEST_COMPLETED)
    object.__setattr__(event, "batch_id", smuggled)
    record = logging.LogRecord(
        "safe-scalar-test", logging.INFO, __file__, 1, "", (), None
    )
    record.safe_event = event

    rendered = JsonEventFormatter().format(record)

    assert json.loads(rendered)["event"] == "observability_failure"
    assert canary not in rendered

    records: list[logging.LogRecord] = []

    class RecordingHandler(logging.Handler):
        def emit(self, emitted: logging.LogRecord) -> None:
            records.append(emitted)

    underlying = logging.Logger("safe-scalar-emit-test", level=logging.INFO)
    underlying.addHandler(RecordingHandler())
    with pytest.raises(ValueError) as emit_error:
        SafeEventLogger(underlying).emit(event)
    assert str(emit_error.value) == "invalid log event"
    assert canary not in str(emit_error.value)
    assert records == []

    with pytest.raises(ValueError) as construction_error:
        SafeLogEvent(
            event=SafeLogEventName.HTTP_REQUEST_COMPLETED,
            batch_id=smuggled,
        )
    assert str(construction_error.value) == "invalid log event"
    assert canary not in str(construction_error.value)


def test_handler_failure_is_best_effort_and_non_recursive() -> None:
    class FailingHandler(logging.Handler):
        calls = 0

        def emit(self, record: logging.LogRecord) -> None:
            self.calls += 1
            raise RuntimeError("handler leaked private OCR text")

    handler = FailingHandler()
    underlying = logging.Logger("failing-safe-test", level=logging.INFO)
    underlying.addHandler(handler)
    logger = SafeEventLogger(underlying)

    assert logger.emit(
        SafeLogEvent(event=SafeLogEventName.HTTP_REQUEST_COMPLETED)
    ) is None
    assert handler.calls == 1


def test_emit_accepts_only_a_safe_log_event() -> None:
    logger = SafeEventLogger(logging.Logger("typed-safe-test"))

    with pytest.raises(ValueError, match="^invalid log event$"):
        logger.emit({"event": "http_request_completed"})  # type: ignore[arg-type]


def test_log_dispatcher_is_bounded_nonblocking_and_close_is_bounded() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            del record
            entered.set()
            release.wait()

    underlying = logging.Logger("blocking-dispatch-test", level=logging.INFO)
    underlying.addHandler(BlockingHandler())
    logger = SafeEventLogger(underlying)
    dispatcher = SafeEventLogDispatcher(logger, capacity=2)
    event = SafeLogEvent(event=SafeLogEventName.HTTP_REQUEST_COMPLETED)
    started = time.monotonic()
    for _ in range(20):
        dispatcher.emit(event)
    assert time.monotonic() - started < 0.2
    assert entered.wait(0.2)
    assert dispatcher.pending <= 2
    assert dispatcher.worker_count == 1
    assert dispatcher.dropped > 0

    close_started = time.monotonic()
    dispatcher.close(timeout=0.01)
    assert time.monotonic() - close_started < 0.2
    release.set()
    assert dispatcher.wait_closed(0.2)


def test_log_dispatcher_contains_worker_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher = SafeEventLogDispatcher(
        SafeEventLogger(logging.Logger("start-failure-test"))
    )

    def fail_start(self) -> None:
        del self
        raise RuntimeError("thread-start-private-canary")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    dispatcher.emit(SafeLogEvent(event=SafeLogEventName.HTTP_REQUEST_COMPLETED))
    assert dispatcher.alive_workers == 0
    assert dispatcher.pending == 0
    assert dispatcher.dropped == 1
    dispatcher.close()


def test_log_dispatcher_validates_before_enqueue_and_contains_cancelled_error() -> None:
    calls = 0

    class CancelledSink:
        def emit(self, event: SafeLogEvent) -> None:
            nonlocal calls
            assert type(event) is SafeLogEvent
            calls += 1
            raise asyncio.CancelledError("sink cancellation")

    sink = CancelledSink()
    dispatcher = SafeEventLogDispatcher(sink)
    with pytest.raises(ValueError, match="^invalid log event$"):
        dispatcher.emit({"event": "private"})  # type: ignore[arg-type]
    dispatcher.emit(SafeLogEvent(event=SafeLogEventName.HTTP_REQUEST_COMPLETED))
    assert dispatcher.drain(0.2)
    assert calls == 1
    dispatcher.close()
    assert dispatcher.wait_closed(0.2)
