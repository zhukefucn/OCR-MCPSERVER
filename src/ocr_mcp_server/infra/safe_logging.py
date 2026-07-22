"""Content-free structured event logging."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
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


__all__ = [
    "JsonEventFormatter",
    "SafeEventLogger",
    "SafeLogEvent",
    "SafeLogEventName",
]
