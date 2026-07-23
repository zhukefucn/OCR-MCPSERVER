"""FastAPI application factory."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from contextlib import asynccontextmanager

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
from .infra.safe_logging import (
    JsonEventFormatter,
    SafeEventLogDispatcher,
    SafeEventLogger,
    SafeEventSink,
    nonblocking_safe_event_logger,
)
from .services.health import (
    DependencyStatus,
    ProbeCode,
    ProbeResult,
    ReadinessService,
    ReadinessSnapshot,
)
from .services.observability import (
    DependencyName,
    ObservationDispatcher,
    ObservabilitySink,
    best_effort,
    nonblocking_observability,
)
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
    runtime: object | None = None,
    registry: CollectorRegistry | None = None,
    observability: ObservabilitySink | None = None,
    readiness: ReadinessService | None = None,
    event_logger: SafeEventSink | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Create the HTTP application without starting external services."""

    resolved_settings = settings if settings is not None else load_settings()
    resolved_registry = registry if registry is not None else CollectorRegistry()
    raw_observability = (
        observability
        if observability is not None
        else PrometheusObservability(resolved_registry)
    )
    resolved_observability, owned_dispatcher = nonblocking_observability(
        raw_observability, autostart=False
    )
    raw_event_logger = event_logger if event_logger is not None else _event_logger()
    resolved_logger, owned_log_dispatcher = nonblocking_safe_event_logger(
        raw_event_logger, autostart=False
    )
    resolved_gateway = (
        getattr(runtime, "document_gateway") if runtime is not None else gateway
    )
    resolved_readiness = (
        getattr(runtime, "readiness")
        if runtime is not None
        else (
            readiness
            if readiness is not None
            else _UnavailableReadiness(resolved_observability)
        )
    )
    mcp_server = create_mcp_server(resolved_gateway)
    mcp_app = mcp_server.http_app(path="/mcp")

    @asynccontextmanager
    async def lifespan(application):
        async with mcp_app.lifespan(application):
            if owned_dispatcher is not None:
                best_effort(owned_dispatcher.activate)
            if owned_log_dispatcher is not None:
                best_effort(owned_log_dispatcher.activate)
            try:
                if runtime is not None:
                    await runtime.start()
                yield
            finally:
                if runtime is not None:
                    await runtime.close()
                if isinstance(resolved_readiness, ReadinessService):
                    best_effort(resolved_readiness.close_observability)
                if owned_dispatcher is not None:
                    best_effort(owned_dispatcher.close)
                if owned_log_dispatcher is not None:
                    best_effort(owned_log_dispatcher.close)

    app = _ObservedFastAPI(
        title="OCR MCP Server",
        routes=[*mcp_app.routes],
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.gateway = resolved_gateway
    app.state.mcp_server = mcp_server
    app.include_router(router)

    app.state.observability_registry = resolved_registry
    app.state.observability = resolved_observability
    app.state.observability_target = raw_observability
    app.state.observability_dispatcher = (
        resolved_observability
        if isinstance(resolved_observability, ObservationDispatcher)
        else None
    )
    app.state.event_logger = resolved_logger
    app.state.event_logger_target = raw_event_logger
    app.state.event_log_dispatcher = (
        resolved_logger
        if isinstance(resolved_logger, SafeEventLogDispatcher)
        else None
    )
    app.state.readiness = resolved_readiness

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


class _UnavailableReadiness:
    """Deterministic default used until Task 13 composes dependency probes."""

    def __init__(self, observability: ObservabilitySink) -> None:
        self._observability = observability

    async def check(self) -> ReadinessSnapshot:
        snapshot = ReadinessSnapshot(
            tuple(
                ProbeResult(
                    dependency,
                    DependencyStatus.UNAVAILABLE,
                    ProbeCode.UNAVAILABLE,
                )
                for dependency in DependencyName
            )
        )
        for result in snapshot.dependencies:
            try:
                self._observability.set_dependency_ready(result.dependency, False)
            except Exception:
                pass
        return snapshot
