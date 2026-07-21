"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from .api import router
from .settings import AppSettings, load_settings


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """Create the HTTP application without starting external services."""

    resolved_settings = settings if settings is not None else load_settings()
    app = FastAPI(title="OCR MCP Server")
    app.state.settings = resolved_settings
    app.include_router(router)
    return app
