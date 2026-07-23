from __future__ import annotations

import asyncio
import gc
import logging
import threading
import time
import weakref
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
import httpx
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from ocr_mcp_server.api.observability import HttpObservabilityMiddleware
from ocr_mcp_server.app import create_app
from ocr_mcp_server.infra.prometheus_observability import PrometheusObservability
from ocr_mcp_server.infra.safe_logging import (
    SafeEventLogDispatcher,
    SafeEventLogger,
    SafeLogEvent,
    SafeLogEventName,
)
from ocr_mcp_server.services.health import (
    DependencyStatus,
    ProbeCode,
    ProbeResult,
    ReadinessService,
)
from ocr_mcp_server.services.observability import (
    DependencyName,
    HttpObservation,
    NullObservability,
    ObservationDispatcher,
    TaskOutcome,
)
from ocr_mcp_server.settings import AppSettings


@dataclass
class RecordingSink:
    observations: list[HttpObservation] = field(default_factory=list)

    def observe_http(self, observation: HttpObservation) -> None:
        self.observations.append(observation)


@dataclass
class RecordingLogger:
    events: list[SafeLogEvent] = field(default_factory=list)

    def emit(self, event: SafeLogEvent) -> None:
        self.events.append(event)


def _observed_app(
    sink: object, logger: object, *, clock: Callable[[], float] | None = None
) -> FastAPI:
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def item(item_id: str) -> PlainTextResponse:
        del item_id
        return PlainTextResponse("ok")

    @app.get("/redirect")
    async def redirect() -> PlainTextResponse:
        return PlainTextResponse("redirect", status_code=302)

    @app.get("/failure")
    async def failure() -> PlainTextResponse:
        return PlainTextResponse("safe failure", status_code=503)

    app.add_middleware(
        HttpObservabilityMiddleware,
        sink=sink,
        logger=logger,
        route_templates=frozenset({"/items/{item_id}", "/redirect", "/failure"}),
        clock=clock or iter((1.0, 1.25)).__next__,
    )
    return app


def test_middleware_uses_routed_templates_and_unmatched_never_raw_paths() -> None:
    sink = RecordingSink()
    logger = RecordingLogger()
    secret_path = "recognized-private-invoice.pdf"
    with TestClient(_observed_app(sink, logger)) as client:
        matched = client.get(f"/items/{secret_path}")
    assert matched.status_code == 200
    assert sink.observations == [
        HttpObservation(
            "GET",
            "/items/{item_id}",
            "2xx",
            0.25,
            route_allowlist=frozenset(
                {"/items/{item_id}", "/redirect", "/failure"}
            ),
        )
    ]
    assert logger.events == [
        SafeLogEvent(
            event=SafeLogEventName.HTTP_REQUEST_COMPLETED, duration_ms=250.0
        )
    ]
    assert secret_path not in repr(sink.observations)
    assert secret_path not in repr(logger.events)


@pytest.mark.parametrize(
    ("path", "expected_status_class", "expected_route"),
    [
        ("/items/id", "2xx", "/items/{item_id}"),
        ("/redirect", "3xx", "/redirect"),
        ("/not-found-private-path", "4xx", "unmatched"),
        ("/failure", "5xx", "/failure"),
    ],
)
def test_middleware_observes_each_final_status_once(
    path: str, expected_status_class: str, expected_route: str
) -> None:
    sink = RecordingSink()
    with TestClient(_observed_app(sink, RecordingLogger())) as client:
        response = client.get(path, follow_redirects=False)
    assert response.status_code // 100 == int(expected_status_class[0])
    assert len(sink.observations) == 1
    assert sink.observations[0].route == expected_route
    assert sink.observations[0].status_class == expected_status_class


def test_metrics_uses_injected_registry_content_type_and_excludes_its_scrape() -> None:
    registry = CollectorRegistry()
    sink = PrometheusObservability(registry)
    settings = AppSettings(auth={"api_keys": []})
    app = create_app(settings, registry=registry, observability=sink)
    with TestClient(app) as client:
        live = client.get("/health/live")
        assert app.state.observability_dispatcher.drain(0.2)
        metrics = client.get("/metrics")
    assert live.status_code == 200
    assert metrics.status_code == 200
    assert metrics.headers["content-type"] == CONTENT_TYPE_LATEST
    assert metrics.content == generate_latest(registry)
    text = metrics.text
    assert 'route="/health/live"' in text
    assert 'route="/metrics"' not in text


@pytest.mark.asyncio
async def test_blocked_sink_does_not_make_metrics_scrapes_block_the_event_loop() -> None:
    release = threading.Event()

    class BlockingSink(RecordingSink):
        def observe_http(self, observation: HttpObservation) -> None:
            release.wait()
            super().observe_http(observation)

    app = create_app(AppSettings(auth={"api_keys": []}), observability=BlockingSink())
    app.state.observability_dispatcher.activate()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health/live")).status_code == 200
            started = time.monotonic()
            ticked = False

            async def tick() -> None:
                nonlocal ticked
                await asyncio.sleep(0)
                ticked = True

            responses = await asyncio.gather(
                *(client.get("/metrics") for _ in range(3)),
                client.get("/health/live"),
                tick(),
            )
            elapsed = time.monotonic() - started
        assert ticked
        assert elapsed < 0.2
        assert all(response.status_code == 200 for response in responses[:-1])
    finally:
        release.set()
        app.state.observability_dispatcher.close()


def test_app_lifespan_closes_only_dispatcher_owned_by_injected_readiness() -> None:
    class Probe:
        def __init__(self, dependency: DependencyName) -> None:
            self.dependency = dependency

        async def check(self) -> ProbeResult:
            return ProbeResult(self.dependency, DependencyStatus.READY, ProbeCode.READY)

    probes = tuple(Probe(dependency) for dependency in DependencyName)
    readiness = ReadinessService(probes, 0.1, observability=RecordingSink())
    owned = readiness._owned_observability
    assert owned is not None
    with TestClient(
        create_app(
            AppSettings(auth={"api_keys": []}),
            observability=NullObservability(),
            readiness=readiness,
        )
    ) as client:
        assert client.get("/health/live").status_code == 200
    assert owned.wait_closed(0.2)

    external = ObservationDispatcher(NullObservability())
    externally_owned_readiness = ReadinessService(
        probes, 0.1, observability=external
    )
    with TestClient(
        create_app(
            AppSettings(auth={"api_keys": []}),
            observability=NullObservability(),
            readiness=externally_owned_readiness,
        )
    ) as client:
        assert client.get("/health/live").status_code == 200
    external.observe_task(TaskOutcome.COMPLETED, 0.1)
    assert external.drain(0.2)
    assert external.alive_workers == 1
    external.close()
    assert external.wait_closed(0.2)


def test_unstarted_unclosed_app_is_collectible_and_terminates_owned_dispatcher() -> None:
    prior_threads = set(threading.enumerate())
    app = create_app(
        AppSettings(auth={"api_keys": []}), observability=RecordingSink()
    )
    app.state.observability_dispatcher.activate()
    app.state.observability.observe_task(TaskOutcome.COMPLETED, 0.1)
    assert app.state.observability_dispatcher.drain(0.2)
    app_reference = weakref.ref(app)
    workers = set(threading.enumerate()) - prior_threads
    assert len(workers) == 1

    del app
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        gc.collect()
        if app_reference() is None and workers.isdisjoint(set(threading.enumerate())):
            break
        time.sleep(0.005)

    assert app_reference() is None
    assert workers.isdisjoint(set(threading.enumerate()))


def test_constructing_apps_without_lifespan_does_not_start_dispatch_workers() -> None:
    prior_workers = {
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("ocr-observation-")
    }
    apps = [
        create_app(AppSettings(auth={"api_keys": []}), observability=RecordingSink())
        for _ in range(8)
    ]
    try:
        current_workers = {
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("ocr-observation-")
        }
        assert current_workers == prior_workers
        assert all(app.state.observability_dispatcher.alive_workers == 0 for app in apps)
    finally:
        apps.clear()
        gc.collect()


def test_request_without_lifespan_does_not_start_app_owned_dispatch_worker() -> None:
    prior_workers = {
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("ocr-observation-")
    }
    app = create_app(
        AppSettings(auth={"api_keys": []}), observability=RecordingSink()
    )
    client = TestClient(app)
    try:
        assert client.get("/health/live").status_code == 200
    finally:
        client.close()
    assert app.state.observability_dispatcher.alive_workers == 0
    assert {
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("ocr-observation-")
    } == prior_workers


def test_repeated_testclient_lifespans_leave_no_app_dispatch_workers() -> None:
    prior_workers = {
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("ocr-observation-")
    }
    for _ in range(6):
        with TestClient(
            create_app(
                AppSettings(auth={"api_keys": []}), observability=RecordingSink()
            )
        ) as client:
            assert client.get("/health/live").status_code == 200
    assert {
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("ocr-observation-")
    } == prior_workers


def test_blocked_logging_handler_never_delays_live_response() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            del record
            entered.set()
            release.wait()

    underlying = logging.Logger("blocked-http-handler", level=logging.INFO)
    underlying.addHandler(BlockingHandler())
    app = create_app(
        AppSettings(auth={"api_keys": []}),
        event_logger=SafeEventLogger(underlying),
    )
    timer = threading.Timer(0.3, release.set)
    timer.start()
    try:
        with TestClient(app) as client:
            started = time.monotonic()
            response = client.get("/health/live")
            elapsed = time.monotonic() - started
            assert response.status_code == 200
            assert elapsed < 0.1
            assert entered.wait(0.2)
    finally:
        release.set()
        timer.cancel()
    dispatcher = app.state.event_log_dispatcher
    assert dispatcher.wait_closed(0.2)


def test_no_lifespan_request_does_not_start_app_log_worker() -> None:
    release = threading.Event()

    class BlockingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            del record
            release.wait()

    underlying = logging.Logger("no-lifespan-log-handler", level=logging.INFO)
    underlying.addHandler(BlockingHandler())
    app = create_app(
        AppSettings(auth={"api_keys": []}),
        event_logger=SafeEventLogger(underlying),
    )
    client = TestClient(app)
    timer = threading.Timer(0.3, release.set)
    timer.start()
    try:
        started = time.monotonic()
        response = client.get("/health/live")
        elapsed = time.monotonic() - started
        assert response.status_code == 200
        assert elapsed < 0.1
        assert app.state.event_log_dispatcher.alive_workers == 0
    finally:
        release.set()
        timer.cancel()
        client.close()


def test_repeated_lifespans_leave_no_app_log_workers_and_external_is_not_closed() -> None:
    prior_workers = {
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("ocr-safe-log-")
    }
    for _ in range(4):
        with TestClient(create_app(AppSettings(auth={"api_keys": []}))) as client:
            assert client.get("/health/live").status_code == 200
    assert {
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("ocr-safe-log-")
    } == prior_workers

    external = SafeEventLogDispatcher(
        SafeEventLogger(logging.Logger("external-log-dispatcher"))
    )
    with TestClient(
        create_app(AppSettings(auth={"api_keys": []}), event_logger=external)
    ) as client:
        assert client.get("/health/live").status_code == 200
    external.emit(SafeLogEvent(event=SafeLogEventName.HTTP_REQUEST_COMPLETED))
    assert external.drain(0.2)
    assert external.alive_workers == 1
    external.close()
    assert external.wait_closed(0.2)


@pytest.mark.asyncio
async def test_app_resources_are_nested_inside_mcp_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeMcpApp:
        routes = []

        @asynccontextmanager
        async def lifespan(self, application):
            del application
            events.append("mcp-enter")
            try:
                yield
            finally:
                events.append("mcp-exit")

    class FakeServer:
        def http_app(self, *, path: str):
            assert path == "/mcp"
            return FakeMcpApp()

    class FakeReadiness:
        def close_observability(self) -> None:
            events.append("readiness-close")

    monkeypatch.setattr("ocr_mcp_server.app.create_mcp_server", lambda gateway: FakeServer())
    monkeypatch.setattr("ocr_mcp_server.app.ReadinessService", FakeReadiness)
    app = create_app(
        AppSettings(auth={"api_keys": []}),
        observability=RecordingSink(),
        event_logger=RecordingLogger(),
        readiness=FakeReadiness(),
    )
    metric_dispatcher = app.state.observability_dispatcher
    log_dispatcher = app.state.event_log_dispatcher
    monkeypatch.setattr(metric_dispatcher, "activate", lambda: events.append("metric-activate"))
    monkeypatch.setattr(metric_dispatcher, "close", lambda: events.append("metric-close"))
    monkeypatch.setattr(log_dispatcher, "activate", lambda: events.append("log-activate"))
    monkeypatch.setattr(log_dispatcher, "close", lambda: events.append("log-close"))

    async with app.router.lifespan_context(app):
        events.append("yield")

    assert events == [
        "mcp-enter",
        "metric-activate",
        "log-activate",
        "yield",
        "readiness-close",
        "metric-close",
        "log-close",
        "mcp-exit",
    ]


@pytest.mark.asyncio
async def test_mcp_enter_failure_does_not_activate_app_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeMcpApp:
        routes = []

        @asynccontextmanager
        async def lifespan(self, application):
            del application
            events.append("mcp-enter")
            raise RuntimeError("safe enter failure")
            yield

    class FakeServer:
        def http_app(self, *, path: str):
            del path
            return FakeMcpApp()

    monkeypatch.setattr("ocr_mcp_server.app.create_mcp_server", lambda gateway: FakeServer())
    app = create_app(
        AppSettings(auth={"api_keys": []}),
        observability=RecordingSink(),
        event_logger=RecordingLogger(),
    )
    monkeypatch.setattr(
        app.state.observability_dispatcher,
        "activate",
        lambda: events.append("metric-activate"),
    )
    monkeypatch.setattr(
        app.state.event_log_dispatcher,
        "activate",
        lambda: events.append("log-activate"),
    )

    with pytest.raises(RuntimeError, match="safe enter failure"):
        async with app.router.lifespan_context(app):
            pass
    assert events == ["mcp-enter"]


@pytest.mark.asyncio
async def test_cleanup_failures_do_not_skip_other_owned_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeMcpApp:
        routes = []

        @asynccontextmanager
        async def lifespan(self, application):
            del application
            events.append("mcp-enter")
            try:
                yield
            finally:
                events.append("mcp-exit")

    class FakeServer:
        def http_app(self, *, path: str):
            del path
            return FakeMcpApp()

    class FakeReadiness:
        def close_observability(self) -> None:
            events.append("readiness-close")
            raise RuntimeError("readiness close")

    def failing_metric_close() -> None:
        events.append("metric-close")
        raise RuntimeError("metric close")

    monkeypatch.setattr("ocr_mcp_server.app.create_mcp_server", lambda gateway: FakeServer())
    monkeypatch.setattr("ocr_mcp_server.app.ReadinessService", FakeReadiness)
    app = create_app(
        AppSettings(auth={"api_keys": []}),
        observability=RecordingSink(),
        event_logger=RecordingLogger(),
        readiness=FakeReadiness(),
    )
    monkeypatch.setattr(app.state.observability_dispatcher, "close", failing_metric_close)
    monkeypatch.setattr(
        app.state.event_log_dispatcher,
        "close",
        lambda: events.append("log-close"),
    )

    async with app.router.lifespan_context(app):
        events.append("yield")
    assert events[-4:] == [
        "readiness-close",
        "metric-close",
        "log-close",
        "mcp-exit",
    ]


@pytest.mark.asyncio
async def test_app_never_activates_or_closes_external_dispatchers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    external_metrics = ObservationDispatcher(NullObservability())
    external_logs = SafeEventLogDispatcher(RecordingLogger())
    monkeypatch.setattr(external_metrics, "activate", lambda: events.append("metric-activate"))
    monkeypatch.setattr(external_metrics, "close", lambda: events.append("metric-close"))
    monkeypatch.setattr(external_logs, "activate", lambda: events.append("log-activate"))
    monkeypatch.setattr(external_logs, "close", lambda: events.append("log-close"))

    app = create_app(
        AppSettings(auth={"api_keys": []}),
        observability=external_metrics,
        event_logger=external_logs,
    )
    async with app.router.lifespan_context(app):
        pass
    assert events == []


def test_metrics_render_failure_is_content_free_503(monkeypatch: pytest.MonkeyPatch) -> None:
    canary = "renderer exception private OCR text invoice.pdf"

    def fail(_registry: CollectorRegistry) -> bytes:
        raise RuntimeError(canary)

    monkeypatch.setattr("ocr_mcp_server.app.generate_latest", fail)
    with TestClient(
        create_app(AppSettings(auth={"api_keys": []}), registry=CollectorRegistry())
    ) as client:
        response = client.get("/metrics")
    assert response.status_code == 503
    assert response.content == b""
    assert canary not in response.text


def test_observability_outputs_exclude_request_canaries() -> None:
    canaries = (
        "recognized customer OCR text",
        "private-invoice.pdf",
        "https://files.example.test/private.pdf",
        r"C:\customers\private.pdf",
        "/srv/ocr/private.pdf",
        "api-key-secret-value",
        "Authorization-Bearer-raw-token",
        "opaque-recovery-token-secret",
        "exception says private filename",
    )
    registry = CollectorRegistry()
    sink = PrometheusObservability(registry)
    logger = RecordingLogger()
    with TestClient(
        create_app(
            AppSettings(auth={"api_keys": []}),
            registry=registry,
            observability=sink,
            event_logger=logger,
        )
    ) as client:
        response = client.get(
            "/" + "-".join(canaries),
            headers={"Authorization": "Bearer " + canaries[-3]},
        )
    assert response.status_code == 503
    outputs = generate_latest(registry).decode() + repr(logger.events) + response.text
    assert all(canary not in outputs for canary in canaries)


def test_unhandled_exception_final_500_is_observed_once_by_outer_boundary() -> None:
    sink = RecordingSink()
    logger = RecordingLogger()
    app = create_app(
        AppSettings(auth={"api_keys": ["a-secure-api-key-0000000000000001"]}),
        registry=CollectorRegistry(),
        observability=sink,
        event_logger=logger,
        clock=iter((1.0, 1.2)).__next__,
    )

    @app.get("/explode")
    async def explode() -> None:
        raise RuntimeError("TOP_SECRET_EXCEPTION_CANARY")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/explode",
            headers={"X-API-Key": "a-secure-api-key-0000000000000001"},
        )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "The request could not be completed.",
        }
    }
    assert len(sink.observations) == 1
    assert sink.observations[0].status_class == "5xx"
    assert len(logger.events) == 1
    outputs = response.text + repr(sink.observations) + repr(logger.events)
    assert "TOP_SECRET_EXCEPTION_CANARY" not in outputs


@pytest.mark.parametrize("fail_at", ["start", "end"])
def test_observation_clock_failure_never_changes_live_response(fail_at: str) -> None:
    calls = 0

    def clock():
        nonlocal calls
        calls += 1
        if fail_at == "start" or calls == 2:
            raise RuntimeError("CLOCK_EXCEPTION_PRIVATE_PATH_CANARY")
        return 1.0

    with TestClient(
        create_app(AppSettings(auth={"api_keys": []}), clock=clock),
        raise_server_exceptions=False,
    ) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert "CLOCK_EXCEPTION_PRIVATE_PATH_CANARY" not in response.text


@pytest.mark.asyncio
async def test_middleware_does_not_buffer_streaming_request_or_response() -> None:
    request_chunks = [
        {"type": "http.request", "body": b"one", "more_body": True},
        {"type": "http.request", "body": b"two", "more_body": False},
    ]
    received: list[dict[str, Any]] = []
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        message = request_chunks.pop(0)
        received.append(message)
        return message

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def streaming_app(scope, downstream_receive, downstream_send) -> None:
        assert (await downstream_receive())["body"] == b"one"
        await downstream_send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await downstream_send(
            {"type": "http.response.body", "body": b"alpha", "more_body": True}
        )
        assert (await downstream_receive())["body"] == b"two"
        await downstream_send(
            {"type": "http.response.body", "body": b"omega", "more_body": False}
        )

    sink = RecordingSink()
    middleware = HttpObservabilityMiddleware(
        streaming_app,
        sink=sink,
        logger=RecordingLogger(),
        route_templates=frozenset({"/mcp"}),
        clock=iter((1.0, 1.1)).__next__,
    )
    await middleware(
        {"type": "http", "method": "POST", "path": "/mcp", "route": _Route("/mcp")},
        receive,
        send,
    )

    assert [message["body"] for message in received] == [b"one", b"two"]
    assert [message.get("body") for message in sent[1:]] == [b"alpha", b"omega"]
    assert len(sink.observations) == 1


@pytest.mark.asyncio
async def test_cancellation_is_reraised_after_single_observation() -> None:
    async def cancelled_app(scope, receive, send) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise asyncio.CancelledError

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        del message

    sink = RecordingSink()
    middleware = HttpObservabilityMiddleware(
        cancelled_app,
        sink=sink,
        logger=RecordingLogger(),
        route_templates=frozenset({"/mcp"}),
        clock=iter((1.0, 1.2)).__next__,
    )
    with pytest.raises(asyncio.CancelledError):
        await middleware(
            {"type": "http", "method": "POST", "path": "/mcp", "route": _Route("/mcp")},
            receive,
            send,
        )
    assert len(sink.observations) == 1


@pytest.mark.parametrize("failing", ["sink", "logger"])
def test_observation_failures_do_not_change_business_response(failing: str) -> None:
    class Failure:
        def observe_http(self, observation: HttpObservation) -> None:
            del observation
            raise RuntimeError("private sink exception")

        def emit(self, event: SafeLogEvent) -> None:
            del event
            raise RuntimeError("private logger exception")

    sink: object = Failure() if failing == "sink" else RecordingSink()
    logger: object = Failure() if failing == "logger" else RecordingLogger()
    with TestClient(_observed_app(sink, logger)) as client:
        response = client.get("/items/safe")
    assert response.status_code == 200
    assert response.text == "ok"
    assert "private" not in response.text


class _Route:
    def __init__(self, path: str) -> None:
        self.path = path
