from __future__ import annotations

import importlib
from unittest.mock import Mock


def test_application_and_module_entrypoints_are_importable() -> None:
    app_module = importlib.import_module("ocr_mcp_server.app")
    main_module = importlib.import_module("ocr_mcp_server.__main__")

    assert callable(app_module.create_app)
    assert callable(main_module.main)


def test_uvicorn_entrypoint_disables_raw_access_and_error_logging(monkeypatch):
    main_module = importlib.import_module("ocr_mcp_server.__main__")
    settings = Mock()
    settings.server.host = "127.0.0.1"
    settings.server.port = 8000
    run = Mock()
    runtime = object()
    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(
        main_module, "build_runtime", lambda value, observability, event_logger: runtime
    )
    monkeypatch.setattr(
        main_module,
        "create_app",
        lambda value, **kwargs: (
            "app"
            if kwargs["runtime"] is runtime
            else (_ for _ in ()).throw(AssertionError())
        ),
    )
    monkeypatch.setattr(main_module.uvicorn, "run", run)
    main_module.main()
    assert run.call_args.kwargs["access_log"] is False
    assert run.call_args.kwargs["log_config"] is None
    assert run.call_args.kwargs["log_level"] == "critical"
