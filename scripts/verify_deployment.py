#!/usr/bin/env python3
"""Bounded, content-free Ubuntu deployment verification gate."""

from __future__ import annotations

import argparse
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlsplit
from zipfile import ZipFile, BadZipFile

import httpx

EXPECTED_TOOLS = {
    "parse_documents",
    "get_task_status",
    "reparse_with_page_orientation",
}
MAX_RESPONSE = 2 * 1024 * 1024
MAX_ZIP = 1024**3
MAX_MEMBERS = 20_000
EXIT_OK, EXIT_CONFIG, EXIT_RUNTIME, EXIT_E2E, EXIT_SAFETY = range(5)


class VerificationFailure(RuntimeError):
    def __init__(self, code: str, exit_code: int = EXIT_RUNTIME):
        self.code, self.exit_code = code, exit_code
        super().__init__(code)


def safe_event(stage: str, ok: bool, **fields: int | str | bool) -> str:
    allowed = {"count", "sha256", "image_count", "tool_count", "status"}
    if not stage.replace("_", "").isalnum() or any(key not in allowed for key in fields):
        raise VerificationFailure("unsafe_output", EXIT_SAFETY)
    value = {"stage": stage, "ok": bool(ok), **fields}
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 512:
        raise VerificationFailure("unsafe_output", EXIT_SAFETY)
    return encoded


def _run(args: list[str], *, timeout: float = 30) -> str:
    try:
        result = subprocess.run(
            args, check=True, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        raise VerificationFailure("command_failed") from None
    if len(result.stdout.encode()) > MAX_RESPONSE:
        raise VerificationFailure("command_output")
    return result.stdout


def json_rows(text: str) -> list[dict[str, Any]]:
    if len(text.encode()) > MAX_RESPONSE:
        raise VerificationFailure("command_output")
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return value
    except json.JSONDecodeError:
        try:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
            if rows and all(isinstance(item, dict) for item in rows):
                return rows
        except json.JSONDecodeError:
            pass
    raise VerificationFailure("command_json")


def validate_compose(value: Any) -> None:
    try:
        services = value["services"]
        if set(("ocr-production", "mineru-api", "mineru-vlm")) - set(services):
            raise ValueError
        if "mineru-api" not in services["ocr-production"]["depends_on"]:
            raise ValueError
        if "mineru-vlm" not in services["mineru-api"]["depends_on"]:
            raise ValueError
        if services["ocr-production"].get("ports") is None:
            raise ValueError
        if services["mineru-api"].get("ports") or services["mineru-vlm"].get("ports"):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise VerificationFailure("compose_config", EXIT_CONFIG) from None


def validate_result_zip(
    payload: bytes, *, max_bytes: int = MAX_ZIP, max_members: int = MAX_MEMBERS
) -> int:
    if not payload or len(payload) > max_bytes:
        raise VerificationFailure("artifact_bound", EXIT_E2E)
    try:
        with ZipFile(BytesIO(payload)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > max_members:
                raise VerificationFailure("artifact_members", EXIT_E2E)
            markdown = 0
            total = 0
            for info in infos:
                path = PurePosixPath(info.filename)
                if path.is_absolute() or ".." in path.parts or info.file_size > max_bytes:
                    raise VerificationFailure("artifact_path", EXIT_E2E)
                total += info.file_size
                if total > max_bytes:
                    raise VerificationFailure("artifact_bound", EXIT_E2E)
                if path.suffix.lower() == ".md":
                    if not archive.read(info):
                        raise VerificationFailure("artifact_markdown", EXIT_E2E)
                    markdown += 1
            if markdown < 1:
                raise VerificationFailure("artifact_markdown", EXIT_E2E)
            return markdown
    except BadZipFile:
        raise VerificationFailure("artifact_zip", EXIT_E2E) from None


def _bounded_json(response: httpx.Response) -> Any:
    if len(response.content) > MAX_RESPONSE:
        raise VerificationFailure("http_bound")
    try:
        return response.json()
    except ValueError:
        raise VerificationFailure("http_json") from None


def _mcp_json(response: httpx.Response) -> dict[str, Any]:
    if len(response.content) > MAX_RESPONSE:
        raise VerificationFailure("mcp_bound")
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        raise VerificationFailure("mcp_response")
    value = _bounded_json(response)
    if not isinstance(value, dict):
        raise VerificationFailure("mcp_response")
    return value


def verify_static(root: Path, manifest: Path) -> str:
    compose = json.loads(
        _run(
            ["docker", "compose", "--profile", "production", "config", "--format", "json"]
        )
    )
    validate_compose(compose)
    raw = manifest.read_bytes()
    if not raw or len(raw) > MAX_RESPONSE:
        raise VerificationFailure("model_manifest", EXIT_CONFIG)
    json.loads(raw)
    digest = sha256(raw).hexdigest()
    images = _run(
        ["docker", "compose", "--profile", "production", "images", "--format", "json"]
    )
    rows = json_rows(images)
    if len(rows) < 3 or any(not row.get("ID") for row in rows):
        raise VerificationFailure("image_ids", EXIT_CONFIG)
    return digest


def verify_runtime(base_url: str, api_key: str, timeout: float) -> None:
    ps = _run(
        ["docker", "compose", "--profile", "production", "ps", "--format", "json"]
    )
    rows = json_rows(ps)
    if len(rows) < 3 or any(row.get("State") != "running" for row in rows):
        raise VerificationFailure("dependencies")
    deadline = time.monotonic() + timeout
    headers = {"X-API-Key": api_key}
    with httpx.Client(base_url=base_url, timeout=10, follow_redirects=False) as client:
        while True:
            live = client.get("/health/live")
            ready = client.get("/health/ready")
            if live.status_code == ready.status_code == 200:
                break
            if time.monotonic() >= deadline:
                raise VerificationFailure("readiness")
            time.sleep(1)
        if client.get("/v1/tasks/00000000-0000-0000-0000-000000000000").status_code != 401:
            raise VerificationFailure("auth_boundary")
        init = client.post(
            "/mcp",
            headers={**headers, "Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "verify", "version": "1"}},
            },
        )
        if init.status_code != 200:
            raise VerificationFailure("mcp_initialize")
        session = init.headers.get("mcp-session-id")
        mcp_headers = {**headers, "Accept": "application/json, text/event-stream"}
        if session:
            mcp_headers["Mcp-Session-Id"] = session
        initialized = client.post(
            "/mcp",
            headers=mcp_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        if initialized.status_code not in (200, 202):
            raise VerificationFailure("mcp_initialized")
        tools = client.post(
            "/mcp", headers=mcp_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        body = _mcp_json(tools)
        names = {item["name"] for item in body["result"]["tools"]}
        if names != EXPECTED_TOOLS:
            raise VerificationFailure("mcp_tools")


def verify_e2e(base_url: str, api_key: str, timeout: float) -> None:
    from smoke_mineru import synthetic_pdf

    headers = {"X-API-Key": api_key}
    with httpx.Client(base_url=base_url, timeout=30, follow_redirects=False) as client:
        upload = client.post(
            "/v1/uploads",
            headers={
                **headers, "Content-Type": "application/pdf",
                "X-Document-Name": "synthetic.pdf",
                "Idempotency-Key": "verify-upload-v1",
            },
            content=synthetic_pdf(),
        )
        if upload.status_code not in (200, 201):
            raise VerificationFailure("rest_upload", EXIT_E2E)
        file_id = _bounded_json(upload)["file_id"]
        submit = client.post(
            "/v1/tasks", headers=headers,
            json={"sources": [{"file_id": file_id}], "idempotency_key": "verify-task-v1"},
        )
        if submit.status_code not in (200, 202):
            raise VerificationFailure("rest_submit", EXIT_E2E)
        batch_id = _bounded_json(submit)["batch_id"]
        deadline = time.monotonic() + timeout
        while True:
            status = client.get(f"/v1/tasks/{batch_id}", headers=headers)
            if status.status_code != 200:
                raise VerificationFailure("rest_status", EXIT_E2E)
            value = _bounded_json(status)
            if value["status"] in ("completed", "completed_with_errors", "failed", "cancelled"):
                break
            if time.monotonic() >= deadline:
                raise VerificationFailure("e2e_timeout", EXIT_E2E)
            time.sleep(2)
        if value["status"] not in ("completed", "completed_with_errors") or not value["artifacts"]:
            raise VerificationFailure("e2e_result", EXIT_E2E)
        download_url = value["artifacts"][0]["download_url"]
        parsed = urlsplit(download_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/v1/artifacts/")
        ):
            raise VerificationFailure("artifact_url", EXIT_E2E)
        artifact = client.get(parsed.path, headers=headers)
        if artifact.status_code != 200:
            raise VerificationFailure("artifact_download", EXIT_E2E)
        validate_result_zip(artifact.content)


def verify_isolation(cpu_image: str, gpu_image: str) -> None:
    probe = (
        "import importlib.metadata as m,importlib.util as i,json;"
        "pkgs={d.metadata['Name'].lower() for d in m.distributions() if d.metadata['Name']};"
        "print(json.dumps({'vllm':bool(i.find_spec('vllm')),"
        "'paddlepaddle':'paddlepaddle' in pkgs,"
        "'paddlepaddle_gpu':'paddlepaddle-gpu' in pkgs}))"
    )
    cpu = json.loads(_run(["docker", "run", "--rm", "--network", "none", cpu_image, "python", "-c", probe], timeout=60))
    gpu = json.loads(_run(["docker", "run", "--rm", "--network", "none", gpu_image, "python", "-c", probe], timeout=60))
    if cpu != {
        "vllm": False,
        "paddlepaddle": True,
        "paddlepaddle_gpu": False,
    } or gpu != {
        "vllm": False,
        "paddlepaddle": False,
        "paddlepaddle_gpu": True,
    }:
        raise VerificationFailure("image_isolation")


def verify_logs(api_key: str) -> None:
    logs = _run(["docker", "compose", "logs", "--no-color", "--tail", "200", "ocr-production"])
    encoded = logs.encode()
    if len(encoded) > MAX_RESPONSE or api_key.encode() in encoded or any(len(line) > 4096 for line in logs.splitlines()):
        raise VerificationFailure("log_boundary", EXIT_SAFETY)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("static", "runtime", "e2e", "all"), default="all")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--manifest", type=Path, default=Path("models/pp-structure-v3/model-manifest.json"))
    parser.add_argument("--cpu-image", default="ocr-mcp-server:ppstructure-cpu-dev")
    parser.add_argument("--gpu-image", default="ocr-mcp-server:production-dev")
    parser.add_argument("--timeout-seconds", type=float, default=1200)
    args = parser.parse_args(argv)
    api_key = os.environ.get("OCR_VERIFY_API_KEY", "")
    try:
        if not api_key:
            raise VerificationFailure("api_key_missing", EXIT_CONFIG)
        if args.phase in ("static", "all"):
            digest = verify_static(Path.cwd(), args.manifest)
            print(safe_event("static", True, sha256=digest, image_count=3))
            verify_isolation(args.cpu_image, args.gpu_image)
            print(safe_event("isolation", True, image_count=2))
        if args.phase in ("runtime", "all"):
            verify_runtime(args.base_url, api_key, args.timeout_seconds)
            verify_logs(api_key)
            print(safe_event("runtime", True, tool_count=3))
        if args.phase in ("e2e", "all"):
            verify_e2e(args.base_url, api_key, args.timeout_seconds)
            print(safe_event("e2e", True, count=1))
        return EXIT_OK
    except VerificationFailure as exc:
        print(safe_event(exc.code, False), file=sys.stderr)
        return exc.exit_code
    except Exception:
        print(safe_event("internal", False), file=sys.stderr)
        return EXIT_SAFETY


if __name__ == "__main__":
    raise SystemExit(main())
