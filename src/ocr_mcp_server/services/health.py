"""Strict, content-free readiness orchestration."""

from __future__ import annotations

import asyncio
import math
import weakref
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
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

    def __repr__(self) -> str:
        try:
            if (
                type(self.dependency) is DependencyName
                and type(self.status) is DependencyStatus
                and type(self.code) is ProbeCode
                and (self.status is DependencyStatus.READY)
                == (self.code is ProbeCode.READY)
            ):
                return (
                    "ProbeResult("
                    f"dependency={self.dependency.value!r}, "
                    f"status={self.status.value!r}, code={self.code.value!r})"
                )
        except Exception:
            pass
        return "ProbeResult(invalid)"


@dataclass(frozen=True, slots=True)
class ReadinessSnapshot:
    dependencies: tuple[ProbeResult, ...]

    def __post_init__(self) -> None:
        if (
            type(self.dependencies) is not tuple
            or len(self.dependencies) != len(DependencyName)
        ):
            raise ValueError("invalid readiness snapshot")
        canonical = tuple(
            _canonical_result(item, dependency)
            for dependency, item in zip(
                DependencyName, self.dependencies, strict=True
            )
        )
        if any(item is None for item in canonical):
            raise ValueError("invalid readiness snapshot")
        object.__setattr__(self, "dependencies", canonical)

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

    def __repr__(self) -> str:
        normalized = normalize_snapshot(self)
        return f"ReadinessSnapshot(status={normalized.status.value!r})"


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
        validated = _validated_probes(probes)
        if validated is None:
            raise ValueError("invalid readiness probes")
        self._probes = validated
        self._timeout_seconds = float(timeout_seconds)
        self._observability = (
            observability if observability is not None else NullObservability()
        )
        self._outstanding_probes: dict[
            DependencyName, asyncio.Task[object]
        ] = {}

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
        existing = self._outstanding_probes.get(dependency)
        if existing is not None:
            if not existing.done():
                return _unavailable(dependency, ProbeCode.TIMEOUT)
            _release_registered_probe(weakref.ref(self), dependency, existing)
        try:
            probe_task = asyncio.create_task(probe.check())
        except asyncio.CancelledError:
            raise
        except Exception:
            return _unavailable(dependency, ProbeCode.INVALID_RESPONSE)
        self._outstanding_probes[dependency] = probe_task
        probe_task.add_done_callback(
            partial(_release_registered_probe, weakref.ref(self), dependency)
        )
        try:
            completed, _ = await asyncio.wait(
                {probe_task}, timeout=self._timeout_seconds
            )
        except asyncio.CancelledError:
            await _cancel_or_detach(probe_task)
            raise
        if not completed:
            await _cancel_or_detach(probe_task)
            return _unavailable(dependency, ProbeCode.TIMEOUT)
        try:
            result = probe_task.result()
        except asyncio.CancelledError:
            return _unavailable(dependency, ProbeCode.UNAVAILABLE)
        except Exception:
            return _unavailable(dependency, ProbeCode.UNAVAILABLE)
        canonical = _canonical_result(result, dependency)
        if canonical is None:
            return _unavailable(dependency, ProbeCode.INVALID_RESPONSE)
        return canonical


async def _cancel_or_detach(task: asyncio.Task[object]) -> None:
    """Bound cancellation cleanup without trusting a coroutine to cooperate."""

    task.cancel()
    await asyncio.sleep(0)
    if task.done():
        _consume_probe_task(task)


def _release_registered_probe(
    service_reference: weakref.ReferenceType[ReadinessService],
    dependency: DependencyName,
    task: asyncio.Task[object],
) -> None:
    service = service_reference()
    if (
        service is not None
        and service._outstanding_probes.get(dependency) is task
    ):
        del service._outstanding_probes[dependency]
    _consume_probe_task(task)


def _consume_probe_task(task: asyncio.Task[object]) -> None:
    try:
        task.result()
    except BaseException:
        pass


def _canonical_result(
    value: object, expected_dependency: DependencyName
) -> ProbeResult | None:
    try:
        if (
            type(value) is not ProbeResult
            or type(value.dependency) is not DependencyName
            or value.dependency is not expected_dependency
            or type(value.status) is not DependencyStatus
            or type(value.code) is not ProbeCode
            or (value.status is DependencyStatus.READY)
            != (value.code is ProbeCode.READY)
        ):
            return None
        return ProbeResult(value.dependency, value.status, value.code)
    except Exception:
        return None


def normalize_snapshot(value: object) -> ReadinessSnapshot:
    """Create a safe public snapshot from an otherwise untrusted result."""

    dependencies: object = None
    if type(value) is ReadinessSnapshot:
        try:
            dependencies = value.dependencies
        except Exception:
            pass
    if type(dependencies) is not tuple or len(dependencies) != len(DependencyName):
        return ReadinessSnapshot(
            tuple(
                _unavailable(dependency, ProbeCode.INVALID_RESPONSE)
                for dependency in DependencyName
            )
        )
    normalized = tuple(
        _canonical_result(item, dependency)
        or _unavailable(dependency, ProbeCode.INVALID_RESPONSE)
        for dependency, item in zip(DependencyName, dependencies, strict=True)
    )
    return ReadinessSnapshot(normalized)


def _validated_probes(probes: object) -> tuple[DependencyProbe, ...] | None:
    provided: list[object] = []
    try:
        iterator = iter(probes)  # type: ignore[arg-type]
        for _ in range(len(DependencyName) + 1):
            try:
                provided.append(next(iterator))
            except StopIteration:
                break
        if len(provided) != len(DependencyName):
            return None
        by_dependency: dict[DependencyName, DependencyProbe] = {}
        for probe in provided:
            dependency = getattr(probe, "dependency")
            check = getattr(probe, "check")
            if (
                type(dependency) is not DependencyName
                or dependency in by_dependency
                or not callable(check)
            ):
                return None
            by_dependency[dependency] = probe  # type: ignore[assignment]
        if set(by_dependency) != set(DependencyName):
            return None
        return tuple(by_dependency[name] for name in DependencyName)
    except Exception:
        return None


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
    "normalize_snapshot",
]
