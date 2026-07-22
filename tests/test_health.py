from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from ocr_mcp_server.infra.health_probes import (
    MinerUReadinessProbe,
    PaddleReadinessProbe,
    SqliteReadinessProbe,
)
from ocr_mcp_server.infra.secondary_ocr import SecondaryOcrWorkerLifecycle
from ocr_mcp_server.services.health import (
    DependencyStatus,
    ProbeCode,
    ProbeResult,
    ReadinessService,
)
from ocr_mcp_server.services.observability import DependencyName


class Probe:
    def __init__(self, dependency: DependencyName, check) -> None:
        self.dependency = dependency
        self._check = check

    async def check(self) -> ProbeResult:
        return await self._check()


def result(
    dependency: DependencyName,
    status: DependencyStatus = DependencyStatus.READY,
    code: ProbeCode = ProbeCode.READY,
) -> ProbeResult:
    return ProbeResult(dependency, status, code)


def service(*probes: Probe, timeout: float = 0.2, observability=None):
    return ReadinessService(probes, timeout, observability)


@pytest.mark.asyncio
async def test_all_probes_start_concurrently_and_results_have_fixed_order() -> None:
    started = {dependency: asyncio.Event() for dependency in DependencyName}
    release = asyncio.Event()

    def concurrent_probe(dependency: DependencyName) -> Probe:
        async def check() -> ProbeResult:
            started[dependency].set()
            await release.wait()
            return result(dependency)

        return Probe(dependency, check)

    readiness = service(
        concurrent_probe(DependencyName.PADDLE),
        concurrent_probe(DependencyName.SQLITE),
        concurrent_probe(DependencyName.MINERU),
    )
    pending = asyncio.create_task(readiness.check())
    await asyncio.wait_for(
        asyncio.gather(*(event.wait() for event in started.values())), timeout=0.1
    )
    release.set()

    snapshot = await pending

    assert snapshot.status is DependencyStatus.READY
    assert [item.dependency for item in snapshot.dependencies] == list(DependencyName)


@pytest.mark.asyncio
async def test_completion_order_does_not_change_dependency_order() -> None:
    completion_order: list[DependencyName] = []

    def delayed(dependency: DependencyName, delay: float) -> Probe:
        async def check() -> ProbeResult:
            await asyncio.sleep(delay)
            completion_order.append(dependency)
            return result(dependency)

        return Probe(dependency, check)

    snapshot = await service(
        delayed(DependencyName.SQLITE, 0.03),
        delayed(DependencyName.MINERU, 0.02),
        delayed(DependencyName.PADDLE, 0),
    ).check()

    assert completion_order == [
        DependencyName.PADDLE,
        DependencyName.MINERU,
        DependencyName.SQLITE,
    ]
    assert [item.dependency for item in snapshot.dependencies] == list(DependencyName)


@pytest.mark.asyncio
async def test_each_probe_has_an_independent_timeout() -> None:
    blocker = asyncio.Event()

    async def blocked() -> ProbeResult:
        await blocker.wait()
        raise AssertionError("unreachable")

    async def ready(dependency: DependencyName) -> ProbeResult:
        return result(dependency)

    snapshot = await service(
        Probe(DependencyName.SQLITE, blocked),
        Probe(DependencyName.MINERU, lambda: ready(DependencyName.MINERU)),
        Probe(DependencyName.PADDLE, lambda: ready(DependencyName.PADDLE)),
        timeout=0.01,
    ).check()

    assert snapshot.dependencies == (
        result(DependencyName.SQLITE, DependencyStatus.UNAVAILABLE, ProbeCode.TIMEOUT),
        result(DependencyName.MINERU),
        result(DependencyName.PADDLE),
    )


@pytest.mark.asyncio
async def test_exception_malformed_and_foreign_results_are_content_free() -> None:
    canary = "exception-canary.invalid/private/path"

    async def raises() -> ProbeResult:
        raise RuntimeError(canary)

    async def malformed():
        return {"dependency": canary}

    async def foreign() -> ProbeResult:
        return result(DependencyName.SQLITE)

    snapshot = await service(
        Probe(DependencyName.SQLITE, raises),
        Probe(DependencyName.MINERU, malformed),
        Probe(DependencyName.PADDLE, foreign),
    ).check()

    assert snapshot.dependencies == (
        result(
            DependencyName.SQLITE,
            DependencyStatus.UNAVAILABLE,
            ProbeCode.UNAVAILABLE,
        ),
        result(
            DependencyName.MINERU,
            DependencyStatus.UNAVAILABLE,
            ProbeCode.INVALID_RESPONSE,
        ),
        result(
            DependencyName.PADDLE,
            DependencyStatus.UNAVAILABLE,
            ProbeCode.INVALID_RESPONSE,
        ),
    )
    assert canary not in repr(snapshot)


@pytest.mark.parametrize(
    "probes",
    [
        (),
        (
            Probe(DependencyName.SQLITE, None),
            Probe(DependencyName.MINERU, None),
        ),
        (
            Probe(DependencyName.SQLITE, None),
            Probe(DependencyName.SQLITE, None),
            Probe(DependencyName.PADDLE, None),
        ),
    ],
)
def test_probe_set_rejects_missing_or_duplicates_content_free(probes) -> None:
    with pytest.raises(ValueError) as exc_info:
        service(*probes)
    assert str(exc_info.value) == "invalid readiness probes"


def test_probe_result_rejects_inconsistent_or_non_finite_values() -> None:
    with pytest.raises(ValueError):
        result(DependencyName.SQLITE, DependencyStatus.READY, ProbeCode.TIMEOUT)
    with pytest.raises(ValueError):
        result(
            DependencyName.SQLITE,
            DependencyStatus.UNAVAILABLE,
            ProbeCode.READY,
        )


@pytest.mark.asyncio
async def test_caller_cancellation_cancels_and_awaits_all_probe_tasks() -> None:
    started = [asyncio.Event() for _ in DependencyName]
    finished = [asyncio.Event() for _ in DependencyName]

    def blocked(dependency: DependencyName, index: int) -> Probe:
        async def check() -> ProbeResult:
            started[index].set()
            try:
                await asyncio.Event().wait()
            finally:
                finished[index].set()
            return result(dependency)

        return Probe(dependency, check)

    pending = asyncio.create_task(
        service(*(blocked(dependency, index) for index, dependency in enumerate(DependencyName))).check()
    )
    await asyncio.gather(*(event.wait() for event in started))
    pending.cancel()

    with pytest.raises(asyncio.CancelledError):
        await pending
    assert all(event.is_set() for event in finished)


@pytest.mark.asyncio
async def test_metrics_are_updated_from_normalized_snapshot_and_fail_best_effort() -> None:
    class Sink:
        def __init__(self) -> None:
            self.calls: list[tuple[DependencyName, bool]] = []

        def set_dependency_ready(self, dependency: DependencyName, ready: bool) -> None:
            self.calls.append((dependency, ready))
            if dependency is DependencyName.MINERU:
                raise RuntimeError("metrics-canary")

    async def check(dependency: DependencyName) -> ProbeResult:
        return result(dependency)

    sink = Sink()
    snapshot = await service(
        *(Probe(dependency, lambda dependency=dependency: check(dependency)) for dependency in DependencyName),
        observability=sink,
    ).check()

    assert snapshot.status is DependencyStatus.READY
    assert sink.calls == [(dependency, True) for dependency in DependencyName]


@pytest.mark.asyncio
async def test_sqlite_and_mineru_probes_accept_only_exact_boolean_results() -> None:
    calls: list[str] = []

    async def yes() -> bool:
        calls.append("yes")
        return True

    async def no() -> bool:
        calls.append("no")
        return False

    async def malformed():
        calls.append("malformed")
        return 1

    assert await SqliteReadinessProbe(yes).check() == result(DependencyName.SQLITE)
    assert await MinerUReadinessProbe(no).check() == result(
        DependencyName.MINERU,
        DependencyStatus.UNAVAILABLE,
        ProbeCode.UNAVAILABLE,
    )
    assert await SqliteReadinessProbe(malformed).check() == result(
        DependencyName.SQLITE,
        DependencyStatus.UNAVAILABLE,
        ProbeCode.INVALID_RESPONSE,
    )
    assert calls == ["yes", "no", "malformed"]


@pytest.mark.asyncio
async def test_paddle_probe_only_reads_strict_running_view() -> None:
    @dataclass
    class View:
        lifecycle: object
        owner_thread_alive: object
        accepting_work: object

    assert await PaddleReadinessProbe(
        View(SecondaryOcrWorkerLifecycle.RUNNING, True, True)
    ).check() == result(DependencyName.PADDLE)
    assert await PaddleReadinessProbe(
        View(SecondaryOcrWorkerLifecycle.CREATED, True, True)
    ).check() == result(
        DependencyName.PADDLE,
        DependencyStatus.UNAVAILABLE,
        ProbeCode.UNAVAILABLE,
    )
    assert await PaddleReadinessProbe(View("running", True, True)).check() == result(
        DependencyName.PADDLE,
        DependencyStatus.UNAVAILABLE,
        ProbeCode.INVALID_RESPONSE,
    )
