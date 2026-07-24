"""Command-line entry point for the OCR MCP server."""

from __future__ import annotations

import uvicorn
from prometheus_client import CollectorRegistry

from .app import _event_logger, create_app
from .bootstrap import build_runtime
from .infra.prometheus_observability import PrometheusObservability
from .settings import load_settings


def main() -> None:
    """Load deployment settings and run the Uvicorn server."""

    settings = load_settings()
    registry = CollectorRegistry()
    observability = PrometheusObservability(registry)
    event_logger = _event_logger()
    runtime = build_runtime(settings, observability, event_logger)
    uvicorn.run(
        create_app(
            settings,
            runtime=runtime,
            registry=registry,
            observability=observability,
            event_logger=event_logger,
        ),
        host=settings.server.host,
        port=settings.server.port,
        access_log=False,
        log_config=None,
        log_level="critical",
    )


if __name__ == "__main__":
    main()
