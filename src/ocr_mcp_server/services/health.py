"""Strict, content-free readiness orchestration."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .observability import (
    DependencyName,
    NullObservability,
    ObservabilitySink,
    best_effort,
)


class DependencyStatus(StrEnum):
    READY = "ready"
    UNAVAILABLE = "unavailable"


class ProbeCode(StrEnum):
    READY = "ready"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"


ProbeResultCode = ProbeCode


@dataclass(frozen=True, slots=True)
class ProbeResult:
    dependency: DependencyName
    status: DependencyStatus
    code: ProbeCode

    def __post_init__(self) -> None:
        if (
            type(self.dependency) is not DependencyName
            or type(self.status) is not DependencyStatus
            or type(self.code) is not ProbeCode
            or (self.status is DependencyStatus.READY) != (self.code is ProbeCode.READY)
        ):
            raise ValueError("invalid probe result")


@dataclass(frozen=True, slots=True)
class ReadinessSnapshot:
    dependencies: tuple[ProbeResult, ...]

    def __post_init__(self) -> None:
        if (
            type(self.dependencies) is not tuple
            or len(self.dependencies) != len(DependencyName)
            or any(type(item) is not ProbeResult for item in self.dependencies)
            or tuple(item.dependency for item in self.dependencies)
            != tuple(DependencyName)
        ):
            raise ValueError("invalid readiness snapshot")

    @property
    def status(self) -> DependencyStatus:
        if all(
            result.status is DependencyStatus.READY for result in self.dependencies
        ):
            return DependencyStatus.READY
        return DependencyStatus.UNAVAILABLE

    @property
    def results(self) -> tuple[ProbeResult, ...]:
        """Expose the normalized results under a transport-neutral name."""

        return self.dependencies

    @property
    def ready(self) -> bool:
        return self.status is DependencyStatus.READY


class DependencyProbe(Protocol):
    dependency: DependencyName

    async def check(self) -> ProbeResult: ...


class ReadinessService:
    """Run exactly one bounded probe for every required dependency."""

    def __init__(
        self,
        probes: object,
        timeout_seconds: float,
        observability: ObservabilitySink | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("invalid readiness timeout")
        try:
            provided = tuple(probes)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError("invalid readiness probes") from None
        by_dependency: dict[DependencyName, DependencyProbe] = {}
        for probe in provided:
            dependency = getattr(probe, "dependency", None)
            if (
                type(dependency) is not DependencyName
                or dependency in by_dependency
                or not callable(getattr(probe, "check", None))
            ):
                raise ValueError("invalid readiness probes")
            by_dependency[dependency] = probe
        if set(by_dependency) != set(DependencyName):
            raise ValueError("invalid readiness probes")
        self._probes = tuple(by_dependency[name] for name in DependencyName)
        self._timeout_seconds = float(timeout_seconds)
        self._observability = (
            observability if observability is not None else NullObservability()
        )

    async def check(self) -> ReadinessSnapshot:
        tasks = tuple(
            asyncio.create_task(self._check_one(probe, dependency))
            for dependency, probe in zip(DependencyName, self._probes, strict=True)
        )
        try:
            results = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        snapshot = ReadinessSnapshot(tuple(results))
        for item in snapshot.dependencies:
            best_effort(
                lambda item=item: self._observability.set_dependency_ready(
                    item.dependency, item.status is DependencyStatus.READY
                )
            )
        return snapshot

    async def _check_one(
        self, probe: DependencyProbe, dependency: DependencyName
    ) -> ProbeResult:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                result = await probe.check()
        except TimeoutError:
            return _unavailable(dependency, ProbeCode.TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _unavailable(dependency, ProbeCode.UNAVAILABLE)
        if type(result) is not ProbeResult or result.dependency is not dependency:
            return _unavailable(dependency, ProbeCode.INVALID_RESPONSE)
        return result


def _unavailable(dependency: DependencyName, code: ProbeCode) -> ProbeResult:
    return ProbeResult(dependency, DependencyStatus.UNAVAILABLE, code)


__all__ = [
    "DependencyProbe",
    "DependencyStatus",
    "ProbeCode",
    "ProbeResult",
    "ProbeResultCode",
    "ReadinessService",
    "ReadinessSnapshot",
]
