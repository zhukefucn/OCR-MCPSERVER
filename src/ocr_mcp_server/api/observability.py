"""ASGI HTTP observation boundary."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from ocr_mcp_server.infra.safe_logging import (
    SafeEventLogger,
    SafeLogEvent,
    SafeLogEventName,
)
from ocr_mcp_server.services.observability import (
    HttpObservation,
    ObservabilitySink,
    best_effort,
)


ASGIApp = Callable[
    [
        dict[str, Any],
        Callable[[], Awaitable[dict[str, Any]]],
        Callable[[dict[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]


class HttpObservabilityMiddleware:
    """Observe response metadata without reading request or response bodies."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        sink: ObservabilitySink,
        logger: SafeEventLogger,
        route_templates: frozenset[str],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(route_templates) is not frozenset or not callable(clock):
            raise ValueError("invalid observation")
        self.app = app
        self._sink = sink
        self._logger = logger
        self._route_templates = route_templates
        self._clock = clock

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("path") == "/metrics":
            await self.app(scope, receive, send)
            return

        started_at = self._clock()
        status: int | None = None

        async def observe_send(message: dict[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                candidate = message.get("status")
                if isinstance(candidate, int) and not isinstance(candidate, bool):
                    status = candidate
            await send(message)

        try:
            await self.app(scope, receive, observe_send)
        finally:
            if status is not None:
                self._observe(scope, status, started_at)

    def _observe(self, scope: dict[str, Any], status: int, started_at: float) -> None:
        try:
            duration = max(0.0, self._clock() - started_at)
            route = getattr(scope.get("route"), "path", "unmatched")
            observation = HttpObservation(
                str(scope.get("method", "OTHER")),
                route if isinstance(route, str) else "unmatched",
                _status_class(status),
                duration,
                route_allowlist=self._route_templates,
            )
            best_effort(lambda: self._sink.observe_http(observation))
            best_effort(
                lambda: self._logger.emit(
                    SafeLogEvent(
                        event=SafeLogEventName.HTTP_REQUEST_COMPLETED,
                        duration_ms=duration * 1000,
                    )
                )
            )
        except Exception:
            return


def _status_class(status: int) -> str:
    if 300 <= status < 400:
        return "3xx"
    if 400 <= status < 500:
        return "4xx"
    if status >= 500:
        return "5xx"
    return "2xx"


__all__ = ["HttpObservabilityMiddleware"]
