"""Contract tests for the minimal OCR gateway container assets."""

from __future__ import annotations

import re
import tomllib
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _read_text(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def _compose_config() -> dict[str, Any]:
    loaded = yaml.safe_load(_read_text("compose.yaml"))
    assert isinstance(loaded, dict)
    return loaded


def _dockerignore_patterns() -> list[str]:
    return [
        line.strip()
        for line in _read_text(".dockerignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _dockerignore_excludes(path: str, patterns: list[str]) -> bool:
    """Evaluate the ordered last-match behavior used by our ignore patterns."""

    excluded = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        candidate = pattern[1:] if negated else pattern
        if fnmatchcase(path, candidate):
            excluded = not negated
    return excluded


def _dockerfile_instructions(dockerfile: str) -> list[str]:
    """Join continued lines so Dockerfile directives can be inspected safely."""

    instructions: list[str] = []
    continued_parts: list[str] = []
    for raw_line in dockerfile.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        is_continued = stripped.endswith("\\")
        continued_parts.append(stripped[:-1].rstrip() if is_continued else stripped)
        if not is_continued:
            instructions.append(" ".join(continued_parts))
            continued_parts = []
    return instructions


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
    assert user_directives[-1] == "10001:10001"


def test_gateway_dockerfile_defaults_pip_index_to_official_https_pypi() -> None:
    dockerfile = _read_text("docker/ocr-gateway.Dockerfile")

    pip_index_args = re.findall(
        r"^ARG\s+PIP_INDEX_URL(?:=(\S+))?\s*$",
        dockerfile,
        re.MULTILINE,
    )

    assert pip_index_args == ["https://pypi.org/simple"]
    assert "pypi.tuna.tsinghua.edu.cn" not in dockerfile
    assert "--index-url" not in dockerfile


def test_gateway_dockerfile_exposes_pip_index_arg_to_pip_install_run() -> None:
    dockerfile = _read_text("docker/ocr-gateway.Dockerfile")

    pip_index_arg = re.search(
        r"^ARG\s+PIP_INDEX_URL=https://pypi\.org/simple\s*$",
        dockerfile,
        re.MULTILINE,
    )
    pip_install_run = re.search(
        r"^RUN\s+python -m pip install --no-cache-dir \.\s+\\$",
        dockerfile,
        re.MULTILINE,
    )

    assert pip_index_arg is not None
    assert pip_install_run is not None
    assert pip_index_arg.start() < pip_install_run.start()


def test_gateway_dockerfile_does_not_persist_pip_index_at_runtime() -> None:
    dockerfile = _read_text("docker/ocr-gateway.Dockerfile")
    env_instructions = [
        instruction
        for instruction in _dockerfile_instructions(dockerfile)
        if re.match(r"^ENV(?:\s|$)", instruction, re.IGNORECASE)
    ]

    assert not any(
        re.search(
            r"(?:^|\s)PIP_INDEX_URL\s*(?:=|\s)",
            instruction,
            re.IGNORECASE,
        )
        for instruction in env_instructions
    )


def test_gateway_dockerfile_does_not_set_pip_trusted_host() -> None:
    dockerfile = _read_text("docker/ocr-gateway.Dockerfile")

    assert re.search(r"\bPIP_TRUSTED_HOST\b", dockerfile, re.IGNORECASE) is None


def test_gateway_dockerfile_does_not_use_pip_trusted_host_option() -> None:
    dockerfile = _read_text("docker/ocr-gateway.Dockerfile")

    assert re.search(r"--trusted-host\b", dockerfile, re.IGNORECASE) is None


def test_gateway_dockerfile_uses_only_https_pip_index_values() -> None:
    dockerfile = _read_text("docker/ocr-gateway.Dockerfile")
    pip_index_values = re.findall(
        r"\bPIP_INDEX_URL\s*=\s*([^\s\\]+)",
        dockerfile,
        re.IGNORECASE,
    )

    assert pip_index_values
    assert all(
        value.strip("'\"").lower().startswith("https://")
        for value in pip_index_values
    )


def test_gateway_dockerfile_pip_index_urls_do_not_contain_credentials() -> None:
    dockerfile = _read_text("docker/ocr-gateway.Dockerfile")
    pip_index_values = re.findall(
        r"\bPIP_INDEX_URL\s*=\s*([^\s\\]+)",
        dockerfile,
        re.IGNORECASE,
    )

    assert pip_index_values
    for value in pip_index_values:
        parsed_value = urlsplit(value.strip("'\""))
        assert parsed_value.username is None
        assert parsed_value.password is None


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


def test_runtime_dependencies_exclude_ocr_engines_and_external_services() -> None:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)
    dependencies = pyproject["project"]["dependencies"]
    dependency_names = {
        re.sub(
            r"[-_.]+",
            "-",
            re.split(r"[<>=!~;@\s\[]", dependency, maxsplit=1)[0].lower(),
        )
        for dependency in dependencies
    }

    forbidden_exact_names = {
        "magic-pdf",
        "asyncpg",
        "pg8000",
        "rq",
        "arq",
        "huey",
        "faststream",
        "taskiq",
    }
    forbidden_name_prefixes = (
        "mineru",
        "paddle",
        "torch",
        "redis",
        "postgres",
        "psycopg",
        "celery",
        "dramatiq",
        "kombu",
        "pika",
        "aio-pika",
        "rabbitmq",
    )
    assert dependency_names.isdisjoint(forbidden_exact_names)
    assert not any(
        dependency_name.startswith(forbidden_name_prefixes)
        for dependency_name in dependency_names
    )


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
    patterns = _dockerignore_patterns()

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


def test_dockerignore_keeps_nested_secrets_excluded_after_source_exceptions() -> None:
    patterns = _dockerignore_patterns()
    last_source_exception = patterns.index("!src/**")
    recursive_sensitive_patterns = (
        "**/.env*",
        "**/*.key",
        "**/*.pem",
        "**/*.crt",
        "**/*.cer",
        "**/*.p12",
        "**/*.pfx",
    )

    assert all(
        patterns.index(pattern) > last_source_exception
        for pattern in recursive_sensitive_patterns
    )
    for sensitive_path in (
        "src/.env.production",
        "src/secrets/client.key",
        "src/secrets/client.pem",
        "src/certificates/client.crt",
        "src/certificates/client.cer",
        "src/certificates/client.p12",
        "src/certificates/client.pfx",
    ):
        assert _dockerignore_excludes(sensitive_path, patterns)
    for required_build_input in (
        "config/example.yaml",
        "pyproject.toml",
        "README.md",
        "src/ocr_mcp_server/app.py",
    ):
        assert not _dockerignore_excludes(required_build_input, patterns)


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
