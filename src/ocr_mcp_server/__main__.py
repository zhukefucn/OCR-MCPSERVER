"""Command-line entry point for the OCR MCP server."""

from __future__ import annotations

import uvicorn

from .app import create_app
from .settings import load_settings


def main() -> None:
    """Load deployment settings and run the Uvicorn server."""

    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.server.host,
        port=settings.server.port,
    )


if __name__ == "__main__":
    main()
