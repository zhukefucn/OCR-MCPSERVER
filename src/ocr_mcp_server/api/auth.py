"""Pure ASGI API-key authentication for REST and streaming MCP traffic."""

from __future__ import annotations

import hmac
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from ..settings import AuthenticationSettings


ASGIApp = Callable[
    [dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Awaitable[None]]],
    Awaitable[None],
]
_AUTH_BYPASS_PATHS = frozenset({"/health/live", "/health/ready", "/metrics"})


class ApiKeyAuthMiddleware:
    """Authenticate without buffering request or response bodies."""

    def __init__(self, app: ASGIApp, *, settings: AuthenticationSettings) -> None:
        self.app = app
        self._key_digests = tuple(
            _digest(key.get_secret_value().encode("ascii"))
            for key in settings.api_keys
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in _AUTH_BYPASS_PATHS:
            await self.app(scope, receive, send)
            return
        if not self._key_digests:
            await _send_error(
                send,
                503,
                "authentication_unavailable",
                "Authentication is not configured.",
            )
            return

        headers: dict[bytes, list[bytes]] = {}
        for name, value in scope.get("headers", ()):
            headers.setdefault(name.lower(), []).append(value)
        authorization = headers.get(b"authorization", [])
        api_keys = headers.get(b"x-api-key", [])
        if len(authorization) > 1 or len(api_keys) > 1:
            await _authentication_failed(send)
            return

        bearer: bytes | None = None
        if authorization:
            prefix = b"Bearer "
            if not authorization[0].startswith(prefix):
                await _authentication_failed(send)
                return
            bearer = authorization[0][len(prefix) :]
        header_key = api_keys[0] if api_keys else None
        if (
            bearer is not None
            and header_key is not None
            and not hmac.compare_digest(_digest(bearer), _digest(header_key))
        ):
            await _authentication_failed(send)
            return
        presented_bytes = bearer if bearer is not None else header_key
        if presented_bytes is None:
            await _authentication_failed(send)
            return
        presented_digest = _digest(presented_bytes)
        matched = False
        for configured_digest in self._key_digests:
            matched |= hmac.compare_digest(presented_digest, configured_digest)
        if not matched:
            await _authentication_failed(send)
            return
        await self.app(scope, receive, send)


def _digest(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


async def _authentication_failed(send) -> None:
    await _send_error(
        send,
        401,
        "authentication_failed",
        "Authentication failed.",
        extra_headers=[(b"www-authenticate", b"Bearer")],
    )


async def _send_error(
    send,
    status: int,
    code: str,
    message: str,
    *,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(
        {"error": {"code": code, "message": message}}, separators=(",", ":")
    ).encode("utf-8")
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    headers.extend(extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
