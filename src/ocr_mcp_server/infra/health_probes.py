"""Passive concrete readiness probes for required dependencies."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from .secondary_ocr import SecondaryOcrWorkerLifecycle
from ..services.health import DependencyStatus, ProbeCode, ProbeResult
from ..services.observability import DependencyName


AsyncHealthChecker = Callable[[], Awaitable[object]]


class PaddleReadinessView(Protocol):
    lifecycle: object
    owner_thread_alive: object
    accepting_work: object


class _BooleanReadinessProbe:
    dependency: DependencyName

    def __init__(self, checker: AsyncHealthChecker) -> None:
        if not callable(checker):
            raise ValueError("invalid readiness checker")
        self._checker = checker

    async def check(self) -> ProbeResult:
        checked = await self._checker()
        if type(checked) is not bool:
            return _result(
                self.dependency,
                DependencyStatus.UNAVAILABLE,
                ProbeCode.INVALID_RESPONSE,
            )
        if checked:
            return _result(self.dependency, DependencyStatus.READY, ProbeCode.READY)
        return _result(
            self.dependency,
            DependencyStatus.UNAVAILABLE,
            ProbeCode.UNAVAILABLE,
        )


class SqliteReadinessProbe(_BooleanReadinessProbe):
    """Run an injected bounded, read-only ``SELECT 1`` checker."""

    dependency = DependencyName.SQLITE


class MinerUReadinessProbe(_BooleanReadinessProbe):
    """Run an injected lightweight health checker without submitting work."""

    dependency = DependencyName.MINERU


class PaddleReadinessProbe:
    """Read worker state without starting a worker or loading a model."""

    dependency = DependencyName.PADDLE

    def __init__(self, view: PaddleReadinessView) -> None:
        self._view = view

    async def check(self) -> ProbeResult:
        lifecycle = self._view.lifecycle
        owner_thread_alive = self._view.owner_thread_alive
        accepting_work = self._view.accepting_work
        if (
            type(lifecycle) is not SecondaryOcrWorkerLifecycle
            or type(owner_thread_alive) is not bool
            or type(accepting_work) is not bool
        ):
            return _result(
                self.dependency,
                DependencyStatus.UNAVAILABLE,
                ProbeCode.INVALID_RESPONSE,
            )
        if (
            lifecycle is SecondaryOcrWorkerLifecycle.RUNNING
            and owner_thread_alive
            and accepting_work
        ):
            return _result(self.dependency, DependencyStatus.READY, ProbeCode.READY)
        return _result(
            self.dependency,
            DependencyStatus.UNAVAILABLE,
            ProbeCode.UNAVAILABLE,
        )


def _result(
    dependency: DependencyName, status: DependencyStatus, code: ProbeCode
) -> ProbeResult:
    return ProbeResult(dependency, status, code)


__all__ = [
    "MinerUReadinessProbe",
    "PaddleReadinessProbe",
    "PaddleReadinessView",
    "SqliteReadinessProbe",
]
