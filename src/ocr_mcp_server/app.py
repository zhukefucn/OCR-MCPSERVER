"""FastAPI application factory."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from .api import router
from .api.auth import ApiKeyAuthMiddleware
from .api.gateway import GatewayFailure, GatewayUploadTooLarge
from .api.mcp import create_mcp_server
from .api.observability import HttpObservabilityMiddleware
from .infra.prometheus_observability import PrometheusObservability
from .infra.safe_logging import JsonEventFormatter, SafeEventLogger
from .services.observability import ObservabilitySink
from .settings import AppSettings, load_settings


_SAFE_ROUTE_TEMPLATE = re.compile(
    r"/(?:[A-Za-z][A-Za-z0-9_-]*|\{[A-Za-z][A-Za-z0-9_]*\})"
    r"(?:/(?:[A-Za-z][A-Za-z0-9_-]*|\{[A-Za-z][A-Za-z0-9_]*\}))*\Z"
)


class _ObservedFastAPI(FastAPI):
    def build_middleware_stack(self):
        stack = super().build_middleware_stack()
        configuration = getattr(self, "_http_observability", None)
        if configuration is None:
            return stack
        return HttpObservabilityMiddleware(stack, **configuration)


def create_app(
    settings: AppSettings | None = None,
    *,
    gateway: object | None = None,
    registry: CollectorRegistry | None = None,
    observability: ObservabilitySink | None = None,
    event_logger: SafeEventLogger | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Create the HTTP application without starting external services."""

    resolved_settings = settings if settings is not None else load_settings()
    mcp_server = create_mcp_server(gateway)
    mcp_app = mcp_server.http_app(path="/mcp")
    app = _ObservedFastAPI(
        title="OCR MCP Server",
        routes=[*mcp_app.routes],
        lifespan=mcp_app.lifespan,
    )
    app.state.settings = resolved_settings
    app.state.gateway = gateway
    app.state.mcp_server = mcp_server
    app.include_router(router)

    resolved_registry = registry if registry is not None else CollectorRegistry()
    resolved_observability = (
        observability
        if observability is not None
        else PrometheusObservability(resolved_registry)
    )
    resolved_logger = event_logger if event_logger is not None else _event_logger()
    app.state.observability_registry = resolved_registry
    app.state.observability = resolved_observability

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        try:
            body = generate_latest(resolved_registry)
        except Exception:
            return Response(status_code=503, content=b"")
        return Response(
            content=body, headers={"content-type": CONTENT_TYPE_LATEST}
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        del request, exc
        return _error_response(422, "invalid_request", "The request is invalid.")

    @app.exception_handler(GatewayFailure)
    async def gateway_failure_handler(
        request: Request, exc: GatewayFailure
    ) -> JSONResponse:
        del request
        status_code = 413 if isinstance(exc, GatewayUploadTooLarge) else {
            "not_found": 404,
            "conflict": 409,
            "capacity_exceeded": 429,
            "service_unavailable": 503,
            "invalid_request": 422,
            "unsupported_media_type": 415,
            "orientation_uncertain": 409,
        }.get(exc.code, 500)
        return _error_response(status_code, exc.code, exc.safe_message)

    @app.exception_handler(Exception)
    async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
        del request, exc
        return _error_response(
            500, "internal_error", "The request could not be completed."
        )

    route_templates = frozenset(
        route.path
        for route in app.routes
        if isinstance(getattr(route, "path", None), str)
        and _SAFE_ROUTE_TEMPLATE.fullmatch(route.path) is not None
    )
    app.add_middleware(ApiKeyAuthMiddleware, settings=resolved_settings.auth)
    app._http_observability = {
        "sink": resolved_observability,
        "logger": resolved_logger,
        "route_templates": route_templates,
        "clock": clock,
    }
    return app


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


def _event_logger() -> SafeEventLogger:
    logger = logging.getLogger("ocr_mcp_server.events")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonEventFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return SafeEventLogger(logger)
