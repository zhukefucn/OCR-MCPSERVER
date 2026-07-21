from __future__ import annotations

import importlib


def test_application_and_module_entrypoints_are_importable() -> None:
    app_module = importlib.import_module("ocr_mcp_server.app")
    main_module = importlib.import_module("ocr_mcp_server.__main__")

    assert callable(app_module.create_app)
    assert callable(main_module.main)
