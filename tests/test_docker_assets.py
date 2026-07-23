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


def _dockerfile_stage(dockerfile: str, stage_name: str) -> str:
    stage_headers = list(
        re.finditer(
            r"^FROM\s+\S+(?:\s+AS\s+(\S+))?\s*$",
            dockerfile,
            re.MULTILINE | re.IGNORECASE,
        )
    )
    for index, header in enumerate(stage_headers):
        if header.group(1) == stage_name:
            end = (
                stage_headers[index + 1].start()
                if index + 1 < len(stage_headers)
                else len(dockerfile)
            )
            return dockerfile[header.start() : end]
    raise AssertionError(f"missing Dockerfile stage: {stage_name}")


def test_ppstructure_targets_share_one_paddle_free_gateway_base() -> None:
    dockerfile = _read_text("docker/ocr-gateway-ppstructure.Dockerfile")
    base = _dockerfile_stage(dockerfile, "ppstructure-base")
    cpu = _dockerfile_stage(dockerfile, "ppstructure-cpu")
    gpu = _dockerfile_stage(dockerfile, "ppstructure-gpu")

    assert re.search(
        r"^FROM python:3\.11-slim-bookworm AS ppstructure-base\s*$",
        base,
        re.MULTILINE,
    )
    assert re.search(
        r"^FROM ppstructure-base AS ppstructure-cpu\s*$",
        cpu,
        re.MULTILINE,
    )
    assert re.search(
        r"^FROM ppstructure-base AS ppstructure-gpu\s*$",
        gpu,
        re.MULTILINE,
    )
    assert base.count("python -m pip install .") == 1
    assert "paddlepaddle" not in base.lower()
    assert cpu.count("paddlepaddle==3.3.0") == 1
    assert "paddlepaddle-gpu" not in cpu
    assert gpu.count("paddlepaddle-gpu==3.3.0") == 1
    assert "paddlepaddle==3.3.0" not in gpu


def test_every_ppstructure_pip_install_uses_ephemeral_buildkit_cache_and_bounds() -> None:
    dockerfile = _read_text("docker/ocr-gateway-ppstructure.Dockerfile")
    instructions = _dockerfile_instructions(dockerfile)
    pip_runs = [
        instruction
        for instruction in instructions
        if instruction.startswith("RUN") and "python -m pip install" in instruction
    ]

    assert not dockerfile.startswith("# syntax=")
    assert len(pip_runs) == 3
    assert sum(
        instruction.count("python -m pip install")
        for instruction in pip_runs
    ) == dockerfile.count("python -m pip install")
    assert all(
        instruction.startswith(
            "RUN --mount=type=cache,target=/root/.cache/pip "
        )
        for instruction in pip_runs
    )
    assert "PIP_DEFAULT_TIMEOUT=120" in dockerfile
    assert "PIP_RETRIES=10" in dockerfile
    assert "PIP_NO_CACHE_DIR" not in dockerfile
    assert "--no-cache-dir" not in dockerfile
    assert dockerfile.count("/root/.cache/pip") == len(pip_runs)
    assert not any(
        "/root/.cache/pip" in instruction and not instruction.startswith("RUN")
        for instruction in instructions
    )
    assert not re.search(r"^VOLUME\b.*pip", dockerfile, re.MULTILINE | re.IGNORECASE)


def test_compose_keeps_the_minimal_gateway_and_adds_ppstructure_profiles() -> None:
    compose = _compose_config()

    assert set(compose["services"]) == {
        "ocr-gateway",
        "ocr-gateway-ppstructure-cpu",
        "ocr-gateway-ppstructure-gpu",
        "mineru-api",
        "mineru-vlm",
    }
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

    cpu_gateway = compose["services"]["ocr-gateway-ppstructure-cpu"]
    assert cpu_gateway["profiles"] == ["ppstructure-cpu"]
    assert cpu_gateway["build"] == {
        "context": ".",
        "dockerfile": "docker/ocr-gateway-ppstructure.Dockerfile",
        "target": "ppstructure-cpu",
    }
    assert cpu_gateway["volumes"] == [
        "ocr-data:/data",
        "./config/example.yaml:/app/config/example.yaml:ro",
        "./models/pp-structure-v3:/models:ro",
    ]
    assert cpu_gateway["environment"] == {
        "OCR_SECONDARY_OCR__DEVICE": "cpu",
        "OCR_SECONDARY_OCR__PADDLEX_CONFIG": "/models/pp-structure-v3.yaml",
    }
    assert cpu_gateway["ports"] == ["${OCR_GATEWAY_PORT:-8000}:8000"]
    assert cpu_gateway["init"] is True
    assert cpu_gateway["restart"] == "unless-stopped"

    gpu_gateway = compose["services"]["ocr-gateway-ppstructure-gpu"]
    assert gpu_gateway["profiles"] == ["ppstructure-gpu"]
    assert gpu_gateway["build"] == {
        "context": ".",
        "dockerfile": "docker/ocr-gateway-ppstructure.Dockerfile",
        "target": "ppstructure-gpu",
    }
    assert gpu_gateway["environment"] == {
        "OCR_SECONDARY_OCR__DEVICE": "gpu:0",
        "OCR_SECONDARY_OCR__PADDLEX_CONFIG": "/models/pp-structure-v3.yaml",
    }
    assert gpu_gateway["volumes"] == cpu_gateway["volumes"]
    assert gpu_gateway["ports"] == ["${OCR_GATEWAY_PORT:-8000}:8000"]
    assert gpu_gateway["init"] is True
    assert gpu_gateway["restart"] == "unless-stopped"


def test_mineru_images_are_pinned_fixed_and_offline() -> None:
    api = _read_text("docker/mineru-api.Dockerfile")
    vlm = _read_text("docker/mineru-vlm.Dockerfile")
    combined = f"{api}\n{vlm}".lower()

    assert "mineru==3.2.0" in api
    assert "scripts/mineru_fixed_api.py" in api
    assert '["python", "/app/scripts/mineru_fixed_api.py"]' in api
    assert "vllm/vllm-openai:v0.11.2" in vlm
    assert "mineru[core]==3.2.0" in vlm
    assert "mineru-openai-server" in vlm
    assert "--engine" in vlm and "vllm" in vlm
    assert "--gpu-memory-utilization" in vlm and "0.45" in vlm
    assert "--model" in vlm
    assert "/models/mineru-vlm" in vlm
    assert ":latest" not in combined
    assert ">=" not in combined
    assert "models-download" not in combined


def test_mineru_images_install_and_index_complete_cjk_runtime() -> None:
    for dockerfile in (
        "docker/mineru-api.Dockerfile",
        "docker/mineru-vlm.Dockerfile",
    ):
        source = _read_text(dockerfile).lower()
        for package in (
            "fonts-noto-core",
            "fonts-noto-cjk",
            "fontconfig",
            "libgl1",
            "libglib2.0-0",
        ):
            assert package in source
        assert "apt-get install" in source
        assert "--no-install-recommends" in source
        assert "fc-cache -fv" in source
        assert "rm -rf /var/lib/apt/lists/*" in source
        assert "smoke_mineru.py" in source


def test_mineru_compose_is_internal_fixed_and_least_privilege() -> None:
    compose = _compose_config()
    api = compose["services"]["mineru-api"]
    vlm = compose["services"]["mineru-vlm"]

    assert api["profiles"] == ["mineru"]
    assert vlm["profiles"] == ["mineru"]
    assert "ports" not in api
    assert "ports" not in vlm
    assert api["networks"] == ["ocr-internal"]
    assert vlm["networks"] == ["ocr-internal"]
    assert compose["networks"]["ocr-internal"] == {"internal": True}
    assert api["environment"] == {
        "MINERU_FIXED_BACKEND": "vlm-http-client",
        "MINERU_FIXED_VLM_URL": "http://mineru-vlm:30000",
        "MINERU_UPSTREAM_URL": "http://127.0.0.1:8001",
    }
    assert "/models/mineru-vlm:ro" in vlm["volumes"][0]
    assert vlm["shm_size"] == "4gb"
    assert vlm.get("ipc") != "host"
    assert vlm["deploy"]["resources"]["reservations"]["devices"] == [
        {"driver": "nvidia", "count": 1, "capabilities": ["gpu"]}
    ]
    for service in (api, vlm):
        assert service.get("privileged") is not True
        assert service.get("network_mode") != "host"
        assert "/var/run/docker.sock" not in str(service)
    assert "--allow-public-http-client" not in str(api)


def test_compose_grants_only_the_gpu_profile_one_nvidia_device() -> None:
    services = _compose_config()["services"]
    gpu_gateway = services["ocr-gateway-ppstructure-gpu"]
    assert gpu_gateway["deploy"] == {
        "resources": {
            "reservations": {
                "devices": [
                    {
                        "driver": "nvidia",
                        "count": 1,
                        "capabilities": ["gpu"],
                    }
                ]
            }
        }
    }
    assert "gpus" not in gpu_gateway
    assert "devices" not in gpu_gateway

    for name, gateway in services.items():
        assert gateway.get("privileged") is not True
        assert gateway.get("network_mode") != "host"
        assert "/var/run/docker.sock" not in str(gateway)
        if name not in {"ocr-gateway-ppstructure-gpu", "mineru-vlm"}:
            assert "gpus" not in gateway
            assert "devices" not in gateway
            assert "deploy" not in gateway


def test_ppstructure_cpu_and_gpu_profiles_are_mutually_scoped() -> None:
    services = _compose_config()["services"]
    cpu_profiles = services["ocr-gateway-ppstructure-cpu"]["profiles"]
    gpu_profiles = services["ocr-gateway-ppstructure-gpu"]["profiles"]

    assert cpu_profiles == ["ppstructure-cpu"]
    assert gpu_profiles == ["ppstructure-gpu"]
    assert set(cpu_profiles).isdisjoint(gpu_profiles)


def test_ppstructure_cpu_requirements_are_exact_and_cpu_only() -> None:
    requirements = _read_text("docker/requirements/pp-structure-v3.txt")
    dockerfile = _read_text("docker/ocr-gateway-ppstructure.Dockerfile")
    cpu_stage = _dockerfile_stage(dockerfile, "ppstructure-cpu")
    combined = f"{requirements}\n{cpu_stage}".lower()

    assert re.search(r"^paddleocr\[doc-parser\]==3\.5\.0\s*$", requirements, re.MULTILINE)
    assert "paddlepaddle==3.3.0" in cpu_stage
    assert "https://www.paddlepaddle.org.cn/packages/stable/cpu/" in cpu_stage
    assert "https://pypi.org/simple" in cpu_stage
    assert re.search(r"^ARG\s+(?:PIP|PADDLE).*INDEX", cpu_stage, re.MULTILINE) is None
    index_urls = re.findall(r"--index-url\s+(\S+)", cpu_stage)
    assert index_urls == [
        "https://www.paddlepaddle.org.cn/packages/stable/cpu/",
        "https://pypi.org/simple",
    ]
    for index_url in index_urls:
        parsed_index = urlsplit(index_url)
        assert parsed_index.scheme == "https"
        assert parsed_index.username is None
        assert parsed_index.password is None
    assert ">=" not in requirements
    assert "paddlepaddle-gpu" not in combined
    for forbidden in ("paddleocr-vl", "paddleocr_vl", "vllm", "cuda", "torch"):
        assert forbidden not in combined


def test_ppstructure_gpu_target_is_exact_blackwell_cuda129_runtime() -> None:
    dockerfile = _read_text("docker/ocr-gateway-ppstructure.Dockerfile")
    normalized = dockerfile.lower()
    gpu_stage = _dockerfile_stage(dockerfile, "ppstructure-gpu").lower()

    assert re.search(
        r"^from ppstructure-base as ppstructure-gpu\s*$",
        gpu_stage,
        re.MULTILINE,
    )
    assert "paddlepaddle-gpu==3.3.0" in gpu_stage
    assert "https://www.paddlepaddle.org.cn/packages/stable/cu129/" in gpu_stage
    assert "https://pypi.org/simple" in gpu_stage
    assert "paddleocr[doc-parser]==3.5.0" in _read_text(
        "docker/requirements/pp-structure-v3.txt"
    )
    assert "paddlepaddle==3.3.0" not in gpu_stage
    for forbidden in (
        "cu118",
        "cu11",
        "cu126",
        "cuda:11",
        "cuda:12.6",
        ":latest",
        "paddleocr-vl",
        "paddleocr_vl",
        "vllm",
    ):
        assert forbidden not in gpu_stage

    index_urls = re.findall(r"--index-url\s+(\S+)", gpu_stage)
    assert index_urls == [
        "https://www.paddlepaddle.org.cn/packages/stable/cu129/",
        "https://pypi.org/simple",
    ]
    assert re.findall(r"^user\s+(.+?)\s*$", gpu_stage, re.MULTILINE)[-1] == "10001:10001"
    assert '["ocr-mcp-server"]' in normalized


def test_readme_documents_blackwell_gpu_build_start_and_real_smoke() -> None:
    readme = _read_text("README.md")

    assert "## PP-StructureV3 GPU image" in readme
    assert "CUDA 12.9" in readme
    assert "paddlepaddle-gpu==3.3.0" in readme
    assert "ppstructure-gpu" in readme
    assert (
        "docker compose --profile ppstructure-gpu build "
        "ocr-gateway-ppstructure-gpu"
    ) in readme
    assert (
        "docker compose --profile ppstructure-gpu up -d "
        "ocr-gateway-ppstructure-gpu"
    ) in readme
    assert "--device gpu:0" in readme
    assert "enable_mkldnn" in readme


def test_ppstructure_cpu_dockerfile_is_a_bounded_non_root_gateway_image() -> None:
    dockerfile = _read_text("docker/ocr-gateway-ppstructure.Dockerfile")
    normalized = dockerfile.lower()

    assert re.search(
        r"^from ppstructure-base as ppstructure-cpu\s*$",
        normalized,
        re.MULTILINE,
    )
    assert "COPY pyproject.toml README.md ./" in dockerfile
    assert "COPY src ./src" in dockerfile
    assert "COPY scripts/smoke_pp_structure.py ./scripts/smoke_pp_structure.py" in dockerfile
    assert "COPY scripts/fixtures/pp_structure_smoke.json ./scripts/fixtures/pp_structure_smoke.json" in dockerfile
    assert "python -m pip install ." in dockerfile
    assert ".[dev]" not in dockerfile
    assert re.search(r"(?:--gid|-g)\s+10001\b", normalized)
    assert re.search(r"(?:--uid|-u)\s+10001\b", normalized)
    assert re.search(r"\bmkdir\s+-p\s+/data\s+/models\b", normalized)
    assert re.findall(r"^user\s+(.+?)\s*$", normalized, re.MULTILINE)[-1] == "10001:10001"
    assert re.search(r"^expose 8000\s*$", normalized, re.MULTILINE)
    assert "--interval=30s" in normalized
    assert "--timeout=5s" in normalized
    assert "--start-period=5s" in normalized
    assert "--retries=3" in normalized
    healthcheck = normalized[normalized.index("healthcheck") :]
    assert "urllib.request" in healthcheck
    assert "http://127.0.0.1:8000/health/live" in healthcheck
    assert "curl" not in healthcheck
    assert "wget" not in healthcheck


def test_ppstructure_cpu_profile_has_no_public_paddle_endpoint() -> None:
    cpu_gateway = _compose_config()["services"]["ocr-gateway-ppstructure-cpu"]

    assert cpu_gateway["ports"] == ["${OCR_GATEWAY_PORT:-8000}:8000"]
    assert "expose" not in cpu_gateway
    assert "30000" not in str(cpu_gateway)


def test_ppstructure_cpu_installs_only_required_bookworm_runtime_libraries() -> None:
    dockerfile = _read_text("docker/ocr-gateway-ppstructure.Dockerfile")
    normalized = _dockerfile_stage(dockerfile, "ppstructure-base").lower()
    runtime_install = re.search(
        r"run apt-get update\s+\\\s+&& apt-get install -y --no-install-recommends\s+\\\s+"
        r"libgl1 libglib2\.0-0 libgomp1\s+\\\s+"
        r"&& rm -rf /var/lib/apt/lists/\*",
        normalized,
    )

    assert runtime_install is not None
    assert runtime_install.start() < normalized.index("python -m pip install")
    for forbidden in ("build-essential", "gcc", "g++", "make", "cmake"):
        assert forbidden not in normalized
    assert "/var/cache/apt" not in normalized
    assert normalized.count("apt-get update") == 1


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
    assert "prometheus-client" in dependency_names


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
        "!scripts",
        "!scripts/**",
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


def test_dockerignore_excludes_model_weights_after_script_exceptions() -> None:
    patterns = _dockerignore_patterns()
    last_script_exception = patterns.index("!scripts/**")

    assert patterns.index("models") > last_script_exception
    assert patterns.index("models/**") > last_script_exception
    for model_path in (
        "models/pp-structure-v3/inference.pdiparams",
        "models/pp-structure-v3/model-manifest.json",
    ):
        assert _dockerignore_excludes(model_path, patterns)
    for required_script in (
        "scripts/smoke_pp_structure.py",
        "scripts/fixtures/pp_structure_smoke.json",
    ):
        assert not _dockerignore_excludes(required_script, patterns)


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
