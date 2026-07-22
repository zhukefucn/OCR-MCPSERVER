from __future__ import annotations

import asyncio
from dataclasses import dataclass
import gc
import time
import weakref

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
    ReadinessSnapshot,
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
@pytest.mark.parametrize(
    ("field", "canary"),
    [
        ("dependency", "dependency-canary.invalid"),
        ("status", "status-canary.invalid"),
        ("code", "code-canary.invalid"),
    ],
)
async def test_post_construction_probe_result_mutation_is_normalized_content_free(
    field: str, canary: str
) -> None:
    mutated = result(DependencyName.SQLITE)
    object.__setattr__(mutated, field, canary)
    assert canary not in repr(mutated)

    async def check() -> ProbeResult:
        return mutated

    async def ready(dependency: DependencyName) -> ProbeResult:
        return result(dependency)

    snapshot = await service(
        Probe(DependencyName.SQLITE, check),
        Probe(DependencyName.MINERU, lambda: ready(DependencyName.MINERU)),
        Probe(DependencyName.PADDLE, lambda: ready(DependencyName.PADDLE)),
    ).check()

    assert snapshot.dependencies[0] == result(
        DependencyName.SQLITE,
        DependencyStatus.UNAVAILABLE,
        ProbeCode.INVALID_RESPONSE,
    )
    assert canary not in repr(snapshot)


def test_readiness_snapshot_reconstructs_canonical_probe_result_copies() -> None:
    originals = tuple(result(dependency) for dependency in DependencyName)
    snapshot = ReadinessSnapshot(originals)

    object.__setattr__(originals[0], "code", "copy-canary.invalid")

    assert snapshot.dependencies == tuple(
        result(dependency) for dependency in DependencyName
    )
    assert all(
        canonical is not original
        for canonical, original in zip(snapshot.dependencies, originals, strict=True)
    )
    assert "copy-canary" not in repr(snapshot)


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
async def test_late_ready_after_deadline_is_irrevocably_timeout() -> None:
    cancelled = asyncio.Event()

    async def suppress_cancellation() -> ProbeResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            return result(DependencyName.SQLITE)

    async def ready(dependency: DependencyName) -> ProbeResult:
        return result(dependency)

    snapshot = await service(
        Probe(DependencyName.SQLITE, suppress_cancellation),
        Probe(DependencyName.MINERU, lambda: ready(DependencyName.MINERU)),
        Probe(DependencyName.PADDLE, lambda: ready(DependencyName.PADDLE)),
        timeout=0.01,
    ).check()

    assert cancelled.is_set()
    assert snapshot.dependencies[0] == result(
        DependencyName.SQLITE,
        DependencyStatus.UNAVAILABLE,
        ProbeCode.TIMEOUT,
    )


@pytest.mark.asyncio
async def test_cancellation_suppressing_probe_cannot_delay_timeout_or_metrics() -> None:
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def refuses_cancellation() -> ProbeResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return result(DependencyName.SQLITE)

    async def ready(dependency: DependencyName) -> ProbeResult:
        return result(dependency)

    class Sink:
        def __init__(self) -> None:
            self.calls: list[tuple[DependencyName, bool]] = []

        def set_dependency_ready(self, dependency: DependencyName, ready: bool) -> None:
            self.calls.append((dependency, ready))

    sink = Sink()
    started = time.monotonic()
    pending = asyncio.create_task(
        service(
            Probe(DependencyName.SQLITE, refuses_cancellation),
            Probe(DependencyName.MINERU, lambda: ready(DependencyName.MINERU)),
            Probe(DependencyName.PADDLE, lambda: ready(DependencyName.PADDLE)),
            timeout=0.01,
            observability=sink,
        ).check()
    )
    try:
        await asyncio.wait_for(cancelled.wait(), timeout=0.1)
        await asyncio.sleep(0.02)
        assert pending.done()
        snapshot = await pending
        assert time.monotonic() - started < 0.1
        assert snapshot.dependencies[0].code is ProbeCode.TIMEOUT
        assert sink.calls == [
            (DependencyName.SQLITE, False),
            (DependencyName.MINERU, True),
            (DependencyName.PADDLE, True),
        ]
    finally:
        release.set()
        await asyncio.sleep(0)
    assert sink.calls == [
        (DependencyName.SQLITE, False),
        (DependencyName.MINERU, True),
        (DependencyName.PADDLE, True),
    ]


@pytest.mark.asyncio
async def test_caller_cancellation_returns_promptly_when_probe_refuses_cancellation() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def refuses_cancellation(dependency: DependencyName) -> ProbeResult:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return result(dependency)

    readiness = service(
        *(
            Probe(
                dependency,
                lambda dependency=dependency: refuses_cancellation(dependency),
            )
            for dependency in DependencyName
        ),
        timeout=10,
    )
    pending = asyncio.create_task(readiness.check())
    await started.wait()
    pending.cancel()
    try:
        await asyncio.wait_for(cancelled.wait(), timeout=0.1)
        done, _ = await asyncio.wait({pending}, timeout=0.05)
        assert done == {pending}
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_detached_probe_exception_after_caller_cancellation_is_consumed() -> None:
    canary = "detached-exception-canary.invalid"
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    contexts: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))

    async def refuses_cancellation() -> ProbeResult:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            raise RuntimeError(canary)

    async def ready(dependency: DependencyName) -> ProbeResult:
        return result(dependency)

    pending = asyncio.create_task(
        service(
            Probe(DependencyName.SQLITE, refuses_cancellation),
            Probe(DependencyName.MINERU, lambda: ready(DependencyName.MINERU)),
            Probe(DependencyName.PADDLE, lambda: ready(DependencyName.PADDLE)),
            timeout=10,
        ).check()
    )
    await started.wait()
    pending.cancel()
    try:
        await asyncio.wait_for(cancelled.wait(), timeout=0.1)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=0.1)
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        assert canary not in repr(contexts)
        assert contexts == []
    finally:
        release.set()
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_repeated_checks_keep_one_outstanding_probe_per_dependency() -> None:
    sqlite_calls = 0
    sqlite_active = 0
    release = asyncio.Event()

    async def resistant_sqlite() -> ProbeResult:
        nonlocal sqlite_calls, sqlite_active
        sqlite_calls += 1
        sqlite_active += 1
        try:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                return result(DependencyName.SQLITE)
        finally:
            sqlite_active -= 1

    async def ready(dependency: DependencyName) -> ProbeResult:
        return result(dependency)

    readiness = service(
        Probe(DependencyName.SQLITE, resistant_sqlite),
        Probe(DependencyName.MINERU, lambda: ready(DependencyName.MINERU)),
        Probe(DependencyName.PADDLE, lambda: ready(DependencyName.PADDLE)),
        timeout=0.001,
    )
    try:
        snapshots = [await readiness.check() for _ in range(20)]
        assert sqlite_calls == 1
        assert sqlite_active == 1
        assert all(
            snapshot.dependencies[0].code is ProbeCode.TIMEOUT
            for snapshot in snapshots
        )
    finally:
        release.set()
        for _ in range(10):
            if sqlite_active == 0:
                break
            await asyncio.sleep(0)
    assert sqlite_active == 0


@pytest.mark.asyncio
async def test_concurrent_repeated_checks_are_bounded_and_restart_after_finish() -> None:
    calls = {dependency: 0 for dependency in DependencyName}
    active = {dependency: 0 for dependency in DependencyName}
    release = asyncio.Event()
    recovered = False

    def resistant(dependency: DependencyName) -> Probe:
        async def check() -> ProbeResult:
            calls[dependency] += 1
            if recovered:
                return result(dependency)
            active[dependency] += 1
            try:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()
                    return result(dependency)
            finally:
                active[dependency] -= 1

        return Probe(dependency, check)

    readiness = service(
        *(resistant(dependency) for dependency in DependencyName), timeout=0.005
    )
    try:
        snapshots = await asyncio.gather(*(readiness.check() for _ in range(20)))
        assert sum(calls.values()) == 3
        assert sum(active.values()) == 3
        assert all(
            all(item.code is ProbeCode.TIMEOUT for item in snapshot.dependencies)
            for snapshot in snapshots
        )
    finally:
        release.set()
        for _ in range(10):
            if sum(active.values()) == 0:
                break
            await asyncio.sleep(0)
    assert sum(active.values()) == 0

    recovered = True
    fresh = await readiness.check()
    assert fresh.status is DependencyStatus.READY
    assert calls == {dependency: 2 for dependency in DependencyName}


@pytest.mark.asyncio
async def test_pending_probe_does_not_retain_service_or_metrics_sink() -> None:
    release = asyncio.Event()

    async def resistant_sqlite() -> ProbeResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            return result(DependencyName.SQLITE)

    async def ready(dependency: DependencyName) -> ProbeResult:
        return result(dependency)

    class Sink:
        def set_dependency_ready(self, dependency: DependencyName, ready: bool) -> None:
            del dependency, ready

    sink = Sink()
    readiness = service(
        Probe(DependencyName.SQLITE, resistant_sqlite),
        Probe(DependencyName.MINERU, lambda: ready(DependencyName.MINERU)),
        Probe(DependencyName.PADDLE, lambda: ready(DependencyName.PADDLE)),
        timeout=0.001,
        observability=sink,
    )
    await readiness.check()
    readiness_reference = weakref.ref(readiness)
    sink_reference = weakref.ref(sink)
    del readiness, sink
    gc.collect()
    try:
        assert readiness_reference() is None
        assert sink_reference() is None
    finally:
        release.set()
        await asyncio.sleep(0)


def test_probe_constructor_masks_raising_iterators_and_descriptors() -> None:
    canary = "constructor-canary.invalid/private"

    class RaisingIterable:
        def __iter__(self):
            raise RuntimeError(canary)

    class RaisingProbe:
        @property
        def dependency(self):
            raise RuntimeError(canary)

        async def check(self):
            raise AssertionError("unreachable")

    for probes in (RaisingIterable(), [RaisingProbe()]):
        with pytest.raises(ValueError) as exc_info:
            ReadinessService(probes, 1)
        assert str(exc_info.value) == "invalid readiness probes"
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is None
        assert canary not in repr(exc_info.value)


def test_probe_constructor_inspects_at_most_four_iterable_items() -> None:
    class BoundedInfinite:
        def __init__(self) -> None:
            self.calls = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.calls += 1
            if self.calls > 4:
                raise AssertionError("iterator consumed without a bound")
            return Probe(DependencyName.SQLITE, None)

    probes = BoundedInfinite()
    with pytest.raises(ValueError, match="^invalid readiness probes$"):
        ReadinessService(probes, 1)
    assert probes.calls == 4


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
    readiness = service(
        *(Probe(dependency, lambda dependency=dependency: check(dependency)) for dependency in DependencyName),
        observability=sink,
    )
    snapshot = await readiness.check()
    assert readiness.drain_observations()

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
