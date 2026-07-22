from __future__ import annotations

import tomllib
from pathlib import Path

from ocr_mcp_server.settings import load_settings

ROOT = Path(__file__).parents[1]


def test_project_metadata_declares_python_dependencies_and_entrypoint() -> None:
    pyproject_path = ROOT / "pyproject.toml"
    metadata = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = metadata["project"]

    assert project["requires-python"] == ">=3.11,<3.12"
    assert set(project["scripts"]) == {"ocr-mcp-server"}
    assert project["scripts"]["ocr-mcp-server"] == "ocr_mcp_server.__main__:main"

    runtime_names = {
        dependency.split("<", 1)[0].split(">", 1)[0].split("=", 1)[0].lower()
        for dependency in project["dependencies"]
    }
    assert runtime_names == {
        "fastapi",
        "uvicorn",
        "pydantic",
        "pydantic-settings",
        "pyyaml",
        "sqlalchemy",
        "aiosqlite",
        "httpx",
        "pillow",
        "pypdf",
    }
    development_names = {
        dependency.split("<", 1)[0].split(">", 1)[0].split("=", 1)[0].lower()
        for dependency in project["optional-dependencies"]["dev"]
    }
    assert development_names >= {"pytest", "pytest-asyncio", "respx"}

    metadata_text = pyproject_path.read_text(encoding="utf-8").lower()
    for prohibited in (
        "mineru",
        "paddleocr",
        "paddlex",
        "torch",
        "redis",
        "postgres",
        "rabbitmq",
        "kafka",
    ):
        assert prohibited not in metadata_text


def test_example_yaml_is_valid_and_contains_no_secret_placeholders() -> None:
    example_path = ROOT / "config" / "example.yaml"
    example_text = example_path.read_text(encoding="utf-8")

    settings = load_settings(config_file=example_path)

    assert settings.mineru.backend == "vlm-http-client"
    assert settings.secondary_ocr.engine.value == "pp_structure_v3"
    assert "api_key" not in example_text.lower()
    assert "password" not in example_text.lower()
    assert "secret" not in example_text.lower()


def test_required_repository_skeleton_and_documentation_exist() -> None:
    required_paths = (
        "README.md",
        ".gitignore",
        "src/ocr_mcp_server/infra/__init__.py",
        "src/ocr_mcp_server/services/__init__.py",
        "src/ocr_mcp_server/api/__init__.py",
        "docker/ocr-gateway.Dockerfile",
    )
    assert all((ROOT / path).exists() for path in required_paths)

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Python 3.11" in readme
    assert "OCR_" in readme
    assert "REST" in readme
    assert "MCP" in readme
    assert "服务层" in readme
    assert "仓库骨架" in readme

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for ignored in (".venv/", "__pycache__/", ".pytest_cache/", "data/", ".superpowers/"):
        assert ignored in gitignore
