from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ocr_mcp_server.app import create_app
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
