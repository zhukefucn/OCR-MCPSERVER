from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import pytest

from ocr_mcp_server.bootstrap import RuntimeResources
from ocr_mcp_server.infra.health_probes import PaddleReadinessProbe
from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker
from ocr_mcp_server.services.health import DependencyStatus


class _Lifecycle:
    def __init__(self, name: str, events: list[str], *, fail: bool = False) -> None:
        self.name = name
        self.events = events
        self.fail = fail
        self.starts = 0
        self.closes = 0

    async def start(self) -> None:
        self.starts += 1
        self.events.append(f"start:{self.name}")
        if self.fail:
            raise RuntimeError("content-free startup failure")

    async def close(self) -> None:
        self.closes += 1
        self.events.append(f"close:{self.name}")


class _Engine:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.disposes = 0

    async def dispose(self) -> None:
        self.disposes += 1
        self.events.append("close:sqlite")


class _Client:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.closes = 0

    async def aclose(self) -> None:
        self.closes += 1
        self.events.append("close:mineru-client")


@dataclass
class _Readiness:
    marker: str = "ready"


def _runtime(
    events: list[str],
    *,
    paddle: _Lifecycle | None = None,
    orchestration: _Lifecycle | None = None,
) -> RuntimeResources:
    paddle = paddle or _Lifecycle("paddle", events)
    orchestration = orchestration or _Lifecycle("orchestration", events)

    async def initialize(_engine) -> None:
        events.append("start:sqlite")

    return RuntimeResources(
        engine=_Engine(events),  # type: ignore[arg-type]
        session_factory=object(),
        repositories={},
        mineru_adapter=object(),
        mineru_client=_Client(events),  # type: ignore[arg-type]
        paddle_worker=paddle,
        orchestration=orchestration,
        retention_service=object(),
        orientation_recovery=object(),
        document_gateway=object(),
        readiness=_Readiness(),  # type: ignore[arg-type]
        schema_initializer=initialize,
    )


@pytest.mark.asyncio
async def test_runtime_starts_dependencies_and_closes_in_reverse_order() -> None:
    events: list[str] = []
    runtime = _runtime(events)

    await runtime.start()
    await runtime.close()
    await runtime.close()

    assert events == [
        "start:sqlite",
        "start:paddle",
        "start:orchestration",
        "close:orchestration",
        "close:paddle",
        "close:mineru-client",
        "close:sqlite",
    ]


@pytest.mark.asyncio
async def test_runtime_rolls_back_partial_startup_once() -> None:
    events: list[str] = []
    runtime = _runtime(
        events, orchestration=_Lifecycle("orchestration", events, fail=True)
    )

    with pytest.raises(RuntimeError, match="content-free startup failure"):
        await runtime.start()
    await runtime.close()

    assert events == [
        "start:sqlite",
        "start:paddle",
        "start:orchestration",
        "close:orchestration",
        "close:paddle",
        "close:mineru-client",
        "close:sqlite",
    ]


@pytest.mark.asyncio
async def test_runtime_cancellation_propagates_after_rollback() -> None:
    events: list[str] = []
    entered = asyncio.Event()

    class BlockingLifecycle(_Lifecycle):
        async def start(self) -> None:
            self.starts += 1
            self.events.append(f"start:{self.name}")
            entered.set()
            await asyncio.Event().wait()

    runtime = _runtime(events, orchestration=BlockingLifecycle("orchestration", events))
    startup = asyncio.create_task(runtime.start())
    await entered.wait()
    startup.cancel()

    with pytest.raises(asyncio.CancelledError):
        await startup
    assert events[-4:] == [
        "close:orchestration",
        "close:paddle",
        "close:mineru-client",
        "close:sqlite",
    ]


@pytest.mark.asyncio
async def test_real_single_owner_worker_is_ready_only_while_accepting_work() -> None:
    class Backend:
        def recognize(self, candidate):
            raise AssertionError(candidate)

        def close(self) -> None:
            pass

    worker = SingleOwnerSecondaryOcrWorker(lambda: Backend(), queue_capacity=1)
    probe = PaddleReadinessProbe(worker)
    assert (await probe.check()).status is DependencyStatus.UNAVAILABLE

    await worker.start()
    assert (await probe.check()).status is DependencyStatus.READY

    await worker.close()
    assert (await probe.check()).status is DependencyStatus.UNAVAILABLE
