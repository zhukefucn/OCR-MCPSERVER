"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api import router
from .api.auth import ApiKeyAuthMiddleware
from .api.gateway import GatewayFailure, GatewayUploadTooLarge
from .api.mcp import create_mcp_server
from .settings import AppSettings, load_settings


def create_app(
    settings: AppSettings | None = None, *, gateway: object | None = None
) -> FastAPI:
    """Create the HTTP application without starting external services."""

    resolved_settings = settings if settings is not None else load_settings()
    mcp_server = create_mcp_server(gateway)
    mcp_app = mcp_server.http_app(path="/mcp")
    app = FastAPI(
        title="OCR MCP Server",
        routes=[*mcp_app.routes],
        lifespan=mcp_app.lifespan,
    )
    app.state.settings = resolved_settings
    app.state.gateway = gateway
    app.state.mcp_server = mcp_server
    app.include_router(router)
    app.add_middleware(ApiKeyAuthMiddleware, settings=resolved_settings.auth)

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
    return app


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )
