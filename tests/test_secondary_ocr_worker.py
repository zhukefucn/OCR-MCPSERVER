from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass
import gc
from pathlib import Path
import threading
from queue import Queue

import pytest

from ocr_mcp_server.domain import (
    CandidateReference,
    ImageCandidate,
    MinerUImageFormat,
    OrthogonalAngle,
    SecondaryOCREngine,
    SecondaryOcrResult,
    SecondaryResultKind,
    SecondaryResultState,
)


def _candidate(tmp_path: Path, name: str = "one") -> ImageCandidate:
    path = tmp_path / f"{name}.png"
    path.write_bytes(b"not-read-by-worker")
    return ImageCandidate(
        candidate_id=name,
        file_task_id="task",
        result_version=1,
        sha256=("a" if name == "one" else "b") * 64,
        size_bytes=1,
        image_format=MinerUImageFormat.PNG,
        width=1,
        height=1,
        primary_path=path,
        alias_paths=(path,),
        references=(CandidateReference.standalone_input(),),
        node_type_hints=(),
    )


def _other() -> SecondaryOcrResult:
    return SecondaryOcrResult(
        kind=SecondaryResultKind.OTHER,
        angle=OrthogonalAngle.DEG_0,
        content=None,
        content_format=None,
        confidence=0.9,
        engine=SecondaryOCREngine.PP_STRUCTURE_V3,
        model_versions={"pipeline": "test"},
        state=SecondaryResultState.VALID,
    )


@dataclass
class _Backend:
    entered: threading.Event
    release: threading.Event
    events: list[tuple[str, int, str | None]]

    def recognize(self, candidate: ImageCandidate) -> SecondaryOcrResult:
        self.events.append(("recognize", threading.get_ident(), candidate.candidate_id))
        self.entered.set()
        assert self.release.wait(5)
        return _other()

    def close(self) -> None:
        self.events.append(("close", threading.get_ident(), None))


@pytest.mark.asyncio
async def test_worker_owns_factory_inference_and_close_on_one_thread(
    tmp_path: Path,
) -> None:
    from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker

    events: list[tuple[str, int, str | None]] = []
    entered, release = threading.Event(), threading.Event()

    def factory() -> _Backend:
        events.append(("factory", threading.get_ident(), None))
        return _Backend(entered, release, events)

    worker = SingleOwnerSecondaryOcrWorker(factory, queue_capacity=2)
    await worker.start()
    call = asyncio.create_task(worker.recognize(_candidate(tmp_path)))
    assert await asyncio.to_thread(entered.wait, 2)
    release.set()
    assert (await call).kind is SecondaryResultKind.OTHER
    await worker.close()

    owner_ids = {event[1] for event in events}
    assert len(owner_ids) == 1
    assert owner_ids != {threading.get_ident()}
    assert [event[0] for event in events] == ["factory", "recognize", "close"]
    assert worker.lifecycle.value == "closed"
    assert worker.queue_depth == 0
    assert worker.owner_thread_alive is False


@pytest.mark.asyncio
async def test_worker_serializes_fifo_and_enforces_waiting_queue_bound(
    tmp_path: Path,
) -> None:
    from ocr_mcp_server.domain import SecondaryOcrFailure
    from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker

    events: list[tuple[str, int, str | None]] = []
    entered, release = threading.Event(), threading.Event()
    worker = SingleOwnerSecondaryOcrWorker(
        lambda: _Backend(entered, release, events), queue_capacity=1
    )
    await worker.start()
    first = asyncio.create_task(worker.recognize(_candidate(tmp_path, "one")))
    assert await asyncio.to_thread(entered.wait, 2)
    second = asyncio.create_task(worker.recognize(_candidate(tmp_path, "two")))
    await asyncio.sleep(0)
    assert worker.queue_depth == 1
    with pytest.raises(SecondaryOcrFailure) as exc_info:
        await worker.recognize(_candidate(tmp_path, "three"))
    assert exc_info.value.code == "secondary_ocr_queue_saturated"
    release.set()
    await asyncio.gather(first, second)
    await worker.close()
    assert [entry[2] for entry in events if entry[0] == "recognize"] == ["one", "two"]


@pytest.mark.asyncio
async def test_worker_lifecycle_start_is_idempotent_and_calls_are_guarded(
    tmp_path: Path,
) -> None:
    from ocr_mcp_server.domain import SecondaryOcrFailure
    from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker

    count = 0
    release = threading.Event()
    release.set()

    def factory() -> _Backend:
        nonlocal count
        count += 1
        return _Backend(threading.Event(), release, [])

    worker = SingleOwnerSecondaryOcrWorker(factory, queue_capacity=1)
    with pytest.raises(SecondaryOcrFailure) as before:
        await worker.recognize(_candidate(tmp_path))
    assert before.value.code == "secondary_ocr_not_started"
    await worker.start()
    await worker.start()
    assert count == 1
    await worker.close()
    await worker.close()
    with pytest.raises(SecondaryOcrFailure) as after:
        await worker.recognize(_candidate(tmp_path))
    assert after.value.code == "secondary_ocr_not_started"


@pytest.mark.asyncio
async def test_failed_factory_is_safe_and_leaves_no_accepting_worker(
    tmp_path: Path,
) -> None:
    from ocr_mcp_server.domain import SecondaryOcrFailure
    from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker

    planted = "C:/private/customer.png recognized-secret"

    def factory() -> _Backend:
        raise RuntimeError(planted)

    worker = SingleOwnerSecondaryOcrWorker(factory, queue_capacity=1)
    with pytest.raises(SecondaryOcrFailure) as exc_info:
        await worker.start()
    assert exc_info.value.code == "secondary_ocr_initialization_unavailable"
    assert planted not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert worker.lifecycle.value == "failed"
    assert worker.owner_thread_alive is False
    with pytest.raises(SecondaryOcrFailure):
        await worker.recognize(_candidate(tmp_path))
    await worker.close()


@pytest.mark.asyncio
async def test_cancelled_active_call_finishes_safely_and_worker_remains_usable(
    tmp_path: Path,
) -> None:
    from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker

    events: list[tuple[str, int, str | None]] = []
    entered, release = threading.Event(), threading.Event()
    worker = SingleOwnerSecondaryOcrWorker(
        lambda: _Backend(entered, release, events), queue_capacity=2
    )
    await worker.start()
    call = asyncio.create_task(worker.recognize(_candidate(tmp_path, "one")))
    assert await asyncio.to_thread(entered.wait, 2)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    release.set()
    await asyncio.sleep(0.05)
    assert (await worker.recognize(_candidate(tmp_path, "two"))).state is SecondaryResultState.VALID
    await worker.close()


@pytest.mark.asyncio
async def test_cancelled_queued_call_does_not_corrupt_future_or_queue_accounting(
    tmp_path: Path,
) -> None:
    from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker

    events: list[tuple[str, int, str | None]] = []
    entered, release = threading.Event(), threading.Event()
    worker = SingleOwnerSecondaryOcrWorker(
        lambda: _Backend(entered, release, events), queue_capacity=1
    )
    await worker.start()
    active = asyncio.create_task(worker.recognize(_candidate(tmp_path, "one")))
    assert await asyncio.to_thread(entered.wait, 2)
    queued = asyncio.create_task(worker.recognize(_candidate(tmp_path, "two")))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    release.set()
    await active
    await asyncio.sleep(0.05)
    assert worker.queue_depth == 0
    await worker.close()
    assert [entry[2] for entry in events if entry[0] == "recognize"] == ["one", "two"]


@pytest.mark.asyncio
async def test_cancelled_active_failure_is_consumed_without_event_loop_warning(
    tmp_path: Path,
) -> None:
    from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    contexts: list[dict[str, object]] = []

    class FailingBackend:
        def recognize(self, candidate: ImageCandidate) -> SecondaryOcrResult:
            entered.set()
            assert release.wait(5)
            finished.set()
            raise RuntimeError("private path and recognized text")

        def close(self) -> None:
            return None

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    try:
        worker = SingleOwnerSecondaryOcrWorker(
            lambda: FailingBackend(), queue_capacity=1
        )
        await worker.start()
        call = asyncio.create_task(worker.recognize(_candidate(tmp_path)))
        assert await asyncio.to_thread(entered.wait, 2)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        del call
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        await worker.close()
        gc.collect()
        await asyncio.sleep(0)
        assert contexts == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_cancelled_queued_close_rejection_is_consumed_without_warning(
    tmp_path: Path,
) -> None:
    from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker

    entered = threading.Event()
    release = threading.Event()
    contexts: list[dict[str, object]] = []
    backend = _Backend(entered, release, [])
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    try:
        worker = SingleOwnerSecondaryOcrWorker(lambda: backend, queue_capacity=1)
        await worker.start()
        active = asyncio.create_task(worker.recognize(_candidate(tmp_path, "one")))
        assert await asyncio.to_thread(entered.wait, 2)
        queued = asyncio.create_task(worker.recognize(_candidate(tmp_path, "two")))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        del queued
        close = asyncio.create_task(worker.close())
        await asyncio.sleep(0)
        release.set()
        await active
        await close
        gc.collect()
        await asyncio.sleep(0)
        assert contexts == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_close_rejects_queued_work_and_waits_for_active_call_without_blocking_loop(
    tmp_path: Path,
) -> None:
    from ocr_mcp_server.domain import SecondaryOcrFailure
    from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker

    events: list[tuple[str, int, str | None]] = []
    entered, release = threading.Event(), threading.Event()
    worker = SingleOwnerSecondaryOcrWorker(
        lambda: _Backend(entered, release, events), queue_capacity=2
    )
    await worker.start()
    active = asyncio.create_task(worker.recognize(_candidate(tmp_path, "one")))
    assert await asyncio.to_thread(entered.wait, 2)
    queued = asyncio.create_task(worker.recognize(_candidate(tmp_path, "two")))
    await asyncio.sleep(0)
    close_task = asyncio.create_task(worker.close())
    await asyncio.sleep(0)
    assert not close_task.done()
    with pytest.raises(SecondaryOcrFailure) as exc_info:
        await queued
    assert exc_info.value.code == "secondary_ocr_not_started"
    release.set()
    await active
    await close_task


@pytest.mark.asyncio
async def test_backend_failure_is_safe_and_worker_closes(tmp_path: Path) -> None:
    from ocr_mcp_server.domain import SecondaryOcrFailure
    from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker

    planted = "secret formula /private/input.png"

    class Broken:
        def recognize(self, candidate: ImageCandidate) -> SecondaryOcrResult:
            raise SystemExit(planted)

        def close(self) -> None:
            return None

    worker = SingleOwnerSecondaryOcrWorker(lambda: Broken(), queue_capacity=1)
    await worker.start()
    with pytest.raises(SecondaryOcrFailure) as exc_info:
        await worker.recognize(_candidate(tmp_path))
    assert exc_info.value.code == "secondary_ocr_internal_worker_failure"
    assert planted not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    await worker.close()


@pytest.mark.asyncio
async def test_backend_close_failure_is_safe_and_thread_is_joined() -> None:
    from ocr_mcp_server.domain import SecondaryOcrFailure
    from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker

    planted = "recognized-secret C:/private/customer.png"

    class BrokenClose:
        def recognize(self, candidate: ImageCandidate) -> SecondaryOcrResult:
            return _other()

        def close(self) -> None:
            raise RuntimeError(planted)

    worker = SingleOwnerSecondaryOcrWorker(lambda: BrokenClose(), queue_capacity=1)
    await worker.start()
    with pytest.raises(SecondaryOcrFailure) as exc_info:
        await worker.close()
    assert exc_info.value.code == "secondary_ocr_internal_worker_failure"
    assert planted not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert worker.lifecycle.value == "closed"
    assert worker.owner_thread_alive is False


@pytest.mark.asyncio
async def test_cancelled_start_closes_backend_after_factory_reaches_safe_boundary() -> None:
    from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker

    factory_entered = threading.Event()
    factory_release = threading.Event()
    backend_closed = threading.Event()

    class Backend:
        def recognize(self, candidate: ImageCandidate) -> SecondaryOcrResult:
            return _other()

        def close(self) -> None:
            backend_closed.set()

    def factory() -> Backend:
        factory_entered.set()
        assert factory_release.wait(5)
        return Backend()

    worker = SingleOwnerSecondaryOcrWorker(factory, queue_capacity=1)
    start = asyncio.create_task(worker.start())
    assert await asyncio.to_thread(factory_entered.wait, 2)
    start.cancel()
    await asyncio.sleep(0)
    assert not start.done()
    factory_release.set()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert backend_closed.is_set()
    assert worker.lifecycle.value == "closed"
    assert worker.owner_thread_alive is False


@pytest.mark.asyncio
async def test_close_during_failed_initialization_finishes_closed() -> None:
    from ocr_mcp_server.domain import SecondaryOcrFailure
    from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker

    factory_entered = threading.Event()
    factory_release = threading.Event()

    def factory() -> _Backend:
        factory_entered.set()
        assert factory_release.wait(5)
        raise RuntimeError("private model initialization detail")

    worker = SingleOwnerSecondaryOcrWorker(factory, queue_capacity=1)
    start = asyncio.create_task(worker.start())
    assert await asyncio.to_thread(factory_entered.wait, 2)
    close = asyncio.create_task(worker.close())
    await asyncio.sleep(0)
    factory_release.set()
    with pytest.raises(SecondaryOcrFailure):
        await start
    await close
    assert worker.lifecycle.value == "closed"
    assert worker.owner_thread_alive is False


@pytest.mark.asyncio
async def test_close_rejects_job_dequeued_but_not_active(tmp_path: Path) -> None:
    from ocr_mcp_server.domain import SecondaryOcrFailure
    from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker

    second_dequeued = threading.Event()
    allow_second_transition = threading.Event()

    class PausingQueue(Queue):
        def __init__(self) -> None:
            super().__init__(maxsize=2)
            self.get_count = 0

        def get(self, block: bool = True, timeout: float | None = None):
            item = super().get(block=block, timeout=timeout)
            self.get_count += 1
            if self.get_count == 2:
                second_dequeued.set()
                assert allow_second_transition.wait(5)
            return item

    first_entered = threading.Event()
    release_first = threading.Event()
    events: list[tuple[str, int, str | None]] = []
    worker = SingleOwnerSecondaryOcrWorker(
        lambda: _Backend(first_entered, release_first, events), queue_capacity=2
    )
    worker._queue = PausingQueue()  # type: ignore[attr-defined]
    await worker.start()
    first = asyncio.create_task(worker.recognize(_candidate(tmp_path, "one")))
    assert await asyncio.to_thread(first_entered.wait, 2)
    second = asyncio.create_task(worker.recognize(_candidate(tmp_path, "two")))
    await asyncio.sleep(0)
    release_first.set()
    await first
    assert await asyncio.to_thread(second_dequeued.wait, 2)
    close = asyncio.create_task(worker.close())
    await asyncio.sleep(0)
    allow_second_transition.set()
    with pytest.raises(SecondaryOcrFailure) as exc_info:
        await second
    assert exc_info.value.code == "secondary_ocr_not_started"
    await close
    assert [entry[2] for entry in events if entry[0] == "recognize"] == ["one"]
