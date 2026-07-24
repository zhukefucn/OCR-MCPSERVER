from __future__ import annotations

import asyncio
import gc
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ocr_mcp_server.app import create_app
from ocr_mcp_server.services.health import (
    DependencyStatus,
    ProbeCode,
    ProbeResult,
    ReadinessSnapshot,
)
from ocr_mcp_server.services.observability import DependencyName
from ocr_mcp_server.settings import AppSettings, ServerSettings


def test_create_app_returns_fastapi_and_preserves_injected_settings() -> None:
    settings = AppSettings(server=ServerSettings(port=9123))

    app = create_app(settings)

    assert isinstance(app, FastAPI)
    assert app.state.settings is settings


def test_liveness_endpoint_returns_stable_json() -> None:
    app = create_app(AppSettings())

    with TestClient(app) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


class Readiness:
    def __init__(self, snapshot: ReadinessSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    async def check(self) -> ReadinessSnapshot:
        self.calls += 1
        return self.snapshot


def _snapshot(*, unavailable: DependencyName | None = None) -> ReadinessSnapshot:
    return ReadinessSnapshot(
        tuple(
            ProbeResult(
                dependency,
                DependencyStatus.UNAVAILABLE
                if dependency is unavailable
                else DependencyStatus.READY,
                ProbeCode.UNAVAILABLE
                if dependency is unavailable
                else ProbeCode.READY,
            )
            for dependency in DependencyName
        )
    )


def test_readiness_endpoint_returns_exact_200_and_503_bodies() -> None:
    ready = Readiness(_snapshot())
    unavailable = Readiness(_snapshot(unavailable=DependencyName.MINERU))

    with TestClient(create_app(AppSettings(), readiness=ready)) as client:
        ready_response = client.get("/health/ready")
    with TestClient(create_app(AppSettings(), readiness=unavailable)) as client:
        unavailable_response = client.get("/health/ready")

    assert ready_response.status_code == 200
    assert ready_response.json() == {
        "status": "ready",
        "dependencies": [
            {"dependency": "sqlite", "status": "ready", "code": "ready"},
            {"dependency": "mineru", "status": "ready", "code": "ready"},
            {"dependency": "paddle", "status": "ready", "code": "ready"},
        ],
    }
    assert unavailable_response.status_code == 503
    assert unavailable_response.json() == {
        "status": "unavailable",
        "dependencies": [
            {"dependency": "sqlite", "status": "ready", "code": "ready"},
            {
                "dependency": "mineru",
                "status": "unavailable",
                "code": "unavailable",
            },
            {"dependency": "paddle", "status": "ready", "code": "ready"},
        ],
    }


def test_default_readiness_is_deterministically_unavailable() -> None:
    with TestClient(create_app(AppSettings())) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "dependencies": [
            {
                "dependency": dependency.value,
                "status": "unavailable",
                "code": "unavailable",
            }
            for dependency in DependencyName
        ],
    }


def test_liveness_never_calls_readiness() -> None:
    readiness = Readiness(_snapshot())
    with TestClient(create_app(AppSettings(), readiness=readiness)) as client:
        response = client.get("/health/live")
    assert response.json() == {"status": "ok"}
    assert readiness.calls == 0


def test_injected_runtime_owns_gateway_readiness_and_lifespan() -> None:
    events: list[str] = []
    gateway = object()
    readiness = Readiness(_snapshot())

    class Runtime:
        def __init__(self) -> None:
            self.document_gateway = gateway
            self.readiness = readiness

        async def start(self) -> None:
            events.append("start")

        async def close(self) -> None:
            events.append("close")

    app = create_app(AppSettings(), runtime=Runtime())
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        assert app.state.gateway is gateway
    assert events == ["start", "close"]


@pytest.mark.asyncio
async def test_one_hundred_runtime_lifespans_leave_no_dispatch_threads() -> None:
    def owned_threads():
        return {
            thread
            for thread in threading.enumerate()
            if thread.name.startswith(("ocr-", "secondary-ocr-"))
        }

    baseline = owned_threads()
    calls = 0

    class Runtime:
        document_gateway = None
        readiness = Readiness(_snapshot())

        async def start(self) -> None:
            nonlocal calls
            calls += 1

        async def close(self) -> None:
            nonlocal calls
            calls += 1

    for _ in range(100):
        app = create_app(AppSettings(), runtime=Runtime())
        async with app.router.lifespan_context(app):
            pass
    del app
    for _ in range(100):
        gc.collect()
        current = owned_threads()
        if current <= baseline:
            break
        await asyncio.sleep(0.005)

    assert calls == 200
    assert owned_threads() <= baseline


def test_readiness_api_renormalizes_mutated_injected_snapshot_content_free() -> None:
    canary = "api-canary.invalid/private/path"
    snapshot = _snapshot()
    object.__setattr__(snapshot.dependencies[0], "code", canary)
    assert canary not in repr(snapshot)
    readiness = Readiness(snapshot)

    with TestClient(create_app(AppSettings(), readiness=readiness)) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "dependencies": [
            {
                "dependency": "sqlite",
                "status": "unavailable",
                "code": "invalid_response",
            },
            {"dependency": "mineru", "status": "ready", "code": "ready"},
            {"dependency": "paddle", "status": "ready", "code": "ready"},
        ],
    }
    assert canary not in response.text
