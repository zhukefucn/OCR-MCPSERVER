"""Finite, transport-neutral observability contracts."""

from __future__ import annotations

import math
import re
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
    except Exception:
        return


__all__ = [
    "DependencyName",
    "HttpObservation",
    "NullObservability",
    "ObservabilitySink",
    "RecoveryOutcome",
    "StageOutcome",
    "TaskOutcome",
    "best_effort",
]
