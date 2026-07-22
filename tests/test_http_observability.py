from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from ocr_mcp_server.api.observability import HttpObservabilityMiddleware
from ocr_mcp_server.app import create_app
from ocr_mcp_server.infra.prometheus_observability import PrometheusObservability
from ocr_mcp_server.infra.safe_logging import SafeLogEvent, SafeLogEventName
from ocr_mcp_server.services.observability import HttpObservation
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
    with TestClient(
        create_app(settings, registry=registry, observability=sink)
    ) as client:
        live = client.get("/health/live")
        metrics = client.get("/metrics")
    assert live.status_code == 200
    assert metrics.status_code == 200
    assert metrics.headers["content-type"] == CONTENT_TYPE_LATEST
    assert metrics.content == generate_latest(registry)
    text = metrics.text
    assert 'route="/health/live"' in text
    assert 'route="/metrics"' not in text


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
