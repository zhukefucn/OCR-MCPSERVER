"""Contract tests for the minimal OCR gateway container assets."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _read_text(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def _compose_config() -> dict[str, Any]:
    loaded = yaml.safe_load(_read_text("compose.yaml"))
    assert isinstance(loaded, dict)
    return loaded


def test_compose_defines_only_the_minimal_gateway_service() -> None:
    compose = _compose_config()

    assert set(compose["services"]) == {"ocr-gateway"}
    assert set(compose["volumes"]) == {"ocr-data"}

    gateway = compose["services"]["ocr-gateway"]
    assert gateway["build"] == {
        "context": ".",
        "dockerfile": "docker/ocr-gateway.Dockerfile",
    }
    assert gateway["image"] == "ocr-mcp-server:0.1.0-dev"
    assert gateway["ports"] == ["${OCR_GATEWAY_PORT:-8000}:8000"]
    assert gateway["volumes"] == [
        "ocr-data:/data",
        "./config/example.yaml:/app/config/example.yaml:ro",
    ]
    assert gateway["init"] is True
    assert gateway["restart"] == "unless-stopped"
    assert "healthcheck" not in gateway


def test_compose_does_not_grant_unneeded_host_or_gpu_access() -> None:
    gateway = _compose_config()["services"]["ocr-gateway"]

    assert gateway.get("privileged") is not True
    assert gateway.get("network_mode") != "host"
    assert "gpus" not in gateway
    assert "devices" not in gateway
    assert "/var/run/docker.sock" not in str(gateway)


def test_gateway_dockerfile_builds_the_installed_package_as_non_root() -> None:
    dockerfile = _read_text("docker/ocr-gateway.Dockerfile")
    normalized = dockerfile.lower()

    assert re.search(r"^from python:3\.11-slim-bookworm\s*$", normalized, re.MULTILINE)
    for setting in (
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONUNBUFFERED=1",
        "PIP_NO_CACHE_DIR=1",
    ):
        assert setting in dockerfile
    assert re.search(r"^workdir /app\s*$", normalized, re.MULTILINE)
    assert "COPY pyproject.toml README.md ./" in dockerfile
    assert "COPY src ./src" in dockerfile
    assert "COPY config/example.yaml ./config/example.yaml" in dockerfile
    assert "python -m pip install --no-cache-dir ." in dockerfile
    assert ".[dev]" not in dockerfile

    assert re.search(r"(?:--gid|-g)\s+10001\b", normalized)
    assert re.search(r"(?:--uid|-u)\s+10001\b", normalized)
    assert re.search(r"\bmkdir\s+-p\s+/data\b", normalized)
    user_directives = re.findall(r"^user\s+(.+?)\s*$", normalized, re.MULTILINE)
    assert user_directives
    assert user_directives[-1] not in {"root", "0", "0:0"}


def test_gateway_dockerfile_has_only_gateway_runtime_configuration() -> None:
    dockerfile = _read_text("docker/ocr-gateway.Dockerfile")
    normalized = dockerfile.lower()

    for setting in (
        "OCR_SERVER__HOST=0.0.0.0",
        "OCR_SERVER__PORT=8000",
        "OCR_DATA_ROOT=/data",
        "OCR_CONFIG_FILE=/app/config/example.yaml",
    ):
        assert setting in dockerfile
    assert re.search(r"^expose 8000\s*$", normalized, re.MULTILINE)
    assert '["ocr-mcp-server"]' in dockerfile

    for forbidden_dependency in (
        "mineru",
        "paddleocr",
        "paddlex",
        "torch",
        "redis",
        "postgres",
        "celery",
        "rabbitmq",
    ):
        assert forbidden_dependency not in normalized


def test_gateway_healthcheck_uses_only_the_python_standard_library() -> None:
    dockerfile = _read_text("docker/ocr-gateway.Dockerfile")
    normalized = dockerfile.lower()
    healthcheck = normalized[normalized.index("healthcheck") :]

    assert "python" in healthcheck
    assert "urllib.request" in healthcheck
    assert "http://127.0.0.1:8000/health/live" in healthcheck
    assert "curl" not in healthcheck
    assert "wget" not in healthcheck


def test_dockerignore_excludes_local_state_and_keeps_build_inputs() -> None:
    patterns = {
        line.strip()
        for line in _read_text(".dockerignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    for excluded in (
        ".git",
        ".github",
        ".venv",
        ".superpowers",
        ".pytest_cache",
        ".pytest-tmp",
        "**/__pycache__",
        "build",
        "dist",
        "data",
        ".env*",
        "*.key",
        "*.pem",
        "*.crt",
        "tests",
    ):
        assert excluded in patterns
    for included in (
        "!config/example.yaml",
        "!pyproject.toml",
        "!README.md",
        "!src",
        "!src/**",
    ):
        assert included in patterns


def test_readme_documents_remote_ubuntu_container_verification() -> None:
    readme = _read_text("README.md")

    assert "## 远程 Ubuntu 容器验证" in readme
    assert "本机只运行测试" in readme
    assert "docker compose build ocr-gateway" in readme
    assert "docker compose up -d ocr-gateway" in readme
    assert "docker compose ps" in readme
    assert "http://127.0.0.1:8000/health/live" in readme
    assert "尚未在远程 Ubuntu 完成构建和启动验证" in readme
    assert "仅包含 FastAPI 网关" in readme
    assert "不包含 MinerU 或 Paddle 推理依赖" in readme
