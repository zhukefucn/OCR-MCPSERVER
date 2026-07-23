#!/usr/bin/env python3
"""Bounded, content-free Ubuntu deployment verification gate."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

import httpx

EXPECTED_TOOLS = {
    "parse_documents",
    "get_task_status",
    "reparse_with_page_orientation",
}
DEFAULT_IMAGES = {
    "ocr-production": "ocr-mcp-server:production-dev",
    "mineru-api": "ocr-mcp-server:mineru-api-dev",
    "mineru-vlm": "ocr-mcp-server:mineru-vlm-dev",
}
SYNTHETIC_FILENAME_CANARY = "ocr-verifier-sensitive-name.pdf"
SYNTHETIC_TEXT_CANARY = "Synthetic verification document"
MAX_COMMAND_OUTPUT = 2 * 1024**2
MAX_HTTP_JSON = 2 * 1024**2
MAX_ARTIFACT = 64 * 1024**2
MAX_MANIFEST = 2 * 1024**2
MAX_MEMBERS = 20_000
RUN_ID_PATTERN = re.compile(r"^[a-f0-9]{7,40}-sha256-[a-f0-9]{64}$")
EXIT_OK, EXIT_CONFIG, EXIT_RUNTIME, EXIT_E2E, EXIT_SAFETY = range(5)


class VerificationFailure(RuntimeError):
    def __init__(self, code: str, exit_code: int = EXIT_RUNTIME):
        self.code, self.exit_code = code, exit_code
        super().__init__(code)


def safe_event(stage: str, ok: bool, **fields: int | str | bool) -> str:
    allowed = {"count", "sha256", "image_count", "tool_count", "status"}
    if not stage.replace("_", "").isalnum() or any(key not in allowed for key in fields):
        raise VerificationFailure("unsafe_output", EXIT_SAFETY)
    encoded = json.dumps(
        {"stage": stage, "ok": bool(ok), **fields},
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded) > 512:
        raise VerificationFailure("unsafe_output", EXIT_SAFETY)
    return encoded


def make_run_id(git_sha: str, image_id: str) -> str:
    normalized_image = image_id.removeprefix("sha256:")
    run_id = f"{git_sha}-sha256-{normalized_image}"
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise VerificationFailure("run_id", EXIT_CONFIG)
    return run_id


def validate_run_id(run_id: str, image_id: str) -> None:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise VerificationFailure("run_id", EXIT_CONFIG)
    if run_id.rsplit("-sha256-", 1)[1] != image_id.removeprefix("sha256:"):
        raise VerificationFailure("run_id_candidate", EXIT_CONFIG)


def idempotency_keys(run_id: str) -> tuple[str, str]:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise VerificationFailure("run_id", EXIT_CONFIG)
    upload_key, task_key = f"u-{run_id}", f"t-{run_id}"
    if len(upload_key) > 128 or len(task_key) > 128:
        raise VerificationFailure("run_id", EXIT_CONFIG)
    return upload_key, task_key


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def _run(
    args: list[str],
    *,
    timeout: float = 30,
    max_output: int = MAX_COMMAND_OUTPUT,
) -> str:
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        raise VerificationFailure("command_failed") from None

    stdout = bytearray()
    stderr = bytearray()
    total = 0
    lock = threading.Lock()
    exceeded = threading.Event()

    def drain(pipe: Any, target: bytearray) -> None:
        nonlocal total
        try:
            while not exceeded.is_set():
                chunk = pipe.read(64 * 1024)
                if not chunk:
                    return
                with lock:
                    if total + len(chunk) > max_output:
                        exceeded.set()
                        return
                    total += len(chunk)
                    target.extend(chunk)
        finally:
            pipe.close()

    assert process.stdout is not None and process.stderr is not None
    threads = [
        threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if exceeded.is_set():
                _stop_process(process)
                raise VerificationFailure("command_output")
            if time.monotonic() >= deadline:
                _stop_process(process)
                raise VerificationFailure("command_timeout")
            time.sleep(0.01)
        for thread in threads:
            thread.join(timeout=1)
        if exceeded.is_set():
            raise VerificationFailure("command_output")
        if process.returncode != 0:
            raise VerificationFailure("command_failed")
        try:
            return stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise VerificationFailure("command_encoding") from None
    finally:
        _stop_process(process)
        for thread in threads:
            thread.join(timeout=1)


def json_rows(text: str) -> list[dict[str, Any]]:
    if len(text.encode()) > MAX_COMMAND_OUTPUT:
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


def read_regular_file_bounded(path: Path, *, max_bytes: int) -> bytes:
    try:
        metadata = os.lstat(path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > max_bytes
        ):
            raise VerificationFailure("file_bound", EXIT_CONFIG)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        raise VerificationFailure("file_bound", EXIT_CONFIG) from None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size != metadata.st_size
            or opened.st_size > max_bytes
        ):
            raise VerificationFailure("file_bound", EXIT_CONFIG)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise VerificationFailure("file_bound", EXIT_CONFIG)
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def json_from_chunks(
    chunks: Iterable[bytes], *, max_bytes: int = MAX_HTTP_JSON
) -> Any:
    body = bytearray()
    for chunk in chunks:
        if len(body) + len(chunk) > max_bytes:
            raise VerificationFailure("http_bound")
        body.extend(chunk)
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VerificationFailure("http_json") from None


def mcp_json_from_chunks(
    chunks: Iterable[bytes],
    *,
    request_id: int,
    max_bytes: int = MAX_HTTP_JSON,
) -> dict[str, Any]:
    pending = bytearray()
    total = 0

    def matching(line: bytes) -> dict[str, Any] | None:
        if not line.startswith(b"data:"):
            return None
        value = json.loads(line[5:].strip())
        if isinstance(value, dict) and value.get("id") == request_id:
            return value
        return None

    try:
        for chunk in chunks:
            total += len(chunk)
            if total > max_bytes:
                raise VerificationFailure("mcp_bound")
            pending.extend(chunk)
            while b"\n" in pending:
                line, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                value = matching(line.rstrip(b"\r"))
                if value is not None:
                    return value
        if pending:
            value = matching(bytes(pending))
            if value is not None:
                return value
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VerificationFailure("mcp_response") from None
    raise VerificationFailure("mcp_response")


def _response_json(response: httpx.Response) -> Any:
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) < 0 or int(declared) > MAX_HTTP_JSON:
                raise VerificationFailure("http_bound")
        except ValueError:
            raise VerificationFailure("http_bound") from None
    return json_from_chunks(response.iter_bytes())


def _response_mcp(response: httpx.Response, request_id: int) -> dict[str, Any]:
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) < 0 or int(declared) > MAX_HTTP_JSON:
                raise VerificationFailure("mcp_bound")
        except ValueError:
            raise VerificationFailure("mcp_bound") from None
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        return mcp_json_from_chunks(response.iter_bytes(), request_id=request_id)
    value = _response_json(response)
    if not isinstance(value, dict) or value.get("id") != request_id:
        raise VerificationFailure("mcp_response")
    return value


def validate_initialize_response(
    body: dict[str, Any], *, request_id: int, protocol: str
) -> None:
    result = body.get("result")
    if (
        body.get("jsonrpc") != "2.0"
        or body.get("id") != request_id
        or not isinstance(result, dict)
        or result.get("protocolVersion") != protocol
        or not isinstance(result.get("capabilities"), dict)
        or not isinstance(result.get("serverInfo"), dict)
    ):
        raise VerificationFailure("mcp_initialize")


def stream_to_tempfile(
    chunks: Iterable[bytes], *, max_bytes: int = MAX_ARTIFACT
) -> Path:
    descriptor, name = tempfile.mkstemp(prefix="ocr-verify-", suffix=".zip")
    path = Path(name)
    total = 0
    try:
        with os.fdopen(descriptor, "wb") as stream:
            for chunk in chunks:
                total += len(chunk)
                if total > max_bytes:
                    raise VerificationFailure("artifact_bound", EXIT_E2E)
                stream.write(chunk)
        if total <= 0:
            raise VerificationFailure("artifact_bound", EXIT_E2E)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def validate_result_zip(
    path: Path, *, max_bytes: int = MAX_ARTIFACT, max_members: int = MAX_MEMBERS
) -> int:
    metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > max_bytes
    ):
        raise VerificationFailure("artifact_bound", EXIT_E2E)
    try:
        with ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > max_members:
                raise VerificationFailure("artifact_members", EXIT_E2E)
            markdown = 0
            total = 0
            for info in infos:
                member = PurePosixPath(info.filename)
                if (
                    member.is_absolute()
                    or ".." in member.parts
                    or info.file_size > max_bytes
                ):
                    raise VerificationFailure("artifact_path", EXIT_E2E)
                total += info.file_size
                if total > max_bytes:
                    raise VerificationFailure("artifact_bound", EXIT_E2E)
                if member.suffix.lower() == ".md":
                    if info.file_size <= 0:
                        raise VerificationFailure("artifact_markdown", EXIT_E2E)
                    markdown += 1
            if markdown < 1:
                raise VerificationFailure("artifact_markdown", EXIT_E2E)
            return markdown
    except (BadZipFile, OSError):
        raise VerificationFailure("artifact_zip", EXIT_E2E) from None


def validate_compose(value: Any, expected_images: dict[str, str] | None = None) -> None:
    expected_images = expected_images or DEFAULT_IMAGES
    try:
        services = value["services"]
        if set(expected_images) - set(services):
            raise ValueError
        if "mineru-api" not in services["ocr-production"]["depends_on"]:
            raise ValueError
        if "mineru-vlm" not in services["mineru-api"]["depends_on"]:
            raise ValueError
        if services["ocr-production"].get("ports") is None:
            raise ValueError
        if services["mineru-api"].get("ports") or services["mineru-vlm"].get("ports"):
            raise ValueError
        for service, image in expected_images.items():
            if services[service].get("image") != image:
                raise ValueError
    except (KeyError, TypeError, ValueError):
        raise VerificationFailure("compose_config", EXIT_CONFIG) from None


def _row_image(row: dict[str, Any]) -> str:
    if isinstance(row.get("Image"), str):
        return row["Image"]
    repository, tag = row.get("Repository"), row.get("Tag")
    if isinstance(repository, str) and isinstance(tag, str):
        return f"{repository}:{tag}"
    return ""


def validate_image_rows(
    rows: list[dict[str, Any]], expected_images: dict[str, str]
) -> dict[str, str]:
    by_service = {row.get("Service"): row for row in rows}
    if len(by_service) != len(rows) or set(by_service) != set(expected_images):
        raise VerificationFailure("image_ids", EXIT_CONFIG)
    ids: dict[str, str] = {}
    for service, image in expected_images.items():
        row = by_service[service]
        image_id = row.get("ID")
        if _row_image(row) != image or not isinstance(image_id, str) or not image_id:
            raise VerificationFailure("image_ids", EXIT_CONFIG)
        ids[service] = image_id
    return ids


def validate_runtime_rows(
    rows: list[dict[str, Any]], expected_images: dict[str, str]
) -> None:
    by_service = {row.get("Service"): row for row in rows}
    if len(by_service) != len(rows) or set(by_service) != set(expected_images):
        raise VerificationFailure("dependencies")
    for service, image in expected_images.items():
        row = by_service[service]
        if row.get("State") != "running" or _row_image(row) != image:
            raise VerificationFailure("dependencies")
        if service != "ocr-production" and row.get("Health") != "healthy":
            raise VerificationFailure("dependencies")


def _compose_rows(command: str, expected_images: dict[str, str]) -> list[dict[str, Any]]:
    return json_rows(
        _run(
            [
                "docker",
                "compose",
                "--profile",
                "production",
                command,
                "--format",
                "json",
                *expected_images,
            ]
        )
    )


def candidate_image_ids(expected_images: dict[str, str]) -> dict[str, str]:
    return validate_image_rows(_compose_rows("images", expected_images), expected_images)


def verify_static(manifest: Path, expected_images: dict[str, str]) -> str:
    compose = json.loads(
        _run(
            ["docker", "compose", "--profile", "production", "config", "--format", "json"]
        )
    )
    validate_compose(compose, expected_images)
    raw = read_regular_file_bounded(manifest, max_bytes=MAX_MANIFEST)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise VerificationFailure("model_manifest", EXIT_CONFIG) from None
    if not isinstance(value, dict):
        raise VerificationFailure("model_manifest", EXIT_CONFIG)
    return sha256(raw).hexdigest()


def _stream_status(
    client: httpx.Client, method: str, path: str, **kwargs: Any
) -> int:
    with client.stream(method, path, **kwargs) as response:
        return response.status_code


def _stream_json_request(
    client: httpx.Client, method: str, path: str, **kwargs: Any
) -> tuple[int, Any, dict[str, str]]:
    with client.stream(method, path, **kwargs) as response:
        value = _response_json(response)
        return response.status_code, value, dict(response.headers)


def _stream_mcp_request(
    client: httpx.Client, request_id: int, headers: dict[str, str], payload: dict[str, Any]
) -> tuple[int, dict[str, Any], dict[str, str]]:
    with client.stream("POST", "/mcp", headers=headers, json=payload) as response:
        value = _response_mcp(response, request_id)
        return response.status_code, value, dict(response.headers)


def verify_runtime(
    base_url: str,
    api_key: str,
    timeout: float,
    expected_images: dict[str, str],
) -> None:
    validate_runtime_rows(_compose_rows("ps", expected_images), expected_images)
    deadline = time.monotonic() + timeout
    headers = {"X-API-Key": api_key}
    with httpx.Client(base_url=base_url, timeout=10, follow_redirects=False) as client:
        while True:
            live = _stream_status(client, "GET", "/health/live")
            ready = _stream_status(client, "GET", "/health/ready")
            if live == ready == 200:
                break
            if time.monotonic() >= deadline:
                raise VerificationFailure("readiness")
            time.sleep(1)
        unauthorized = _stream_status(
            client, "GET", "/v1/tasks/00000000-0000-0000-0000-000000000000"
        )
        if unauthorized != 401:
            raise VerificationFailure("auth_boundary")
        accept = {**headers, "Accept": "application/json, text/event-stream"}
        protocol = "2025-03-26"
        status, body, response_headers = _stream_mcp_request(
            client,
            1,
            accept,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": protocol,
                    "capabilities": {},
                    "clientInfo": {"name": "verify", "version": "1"},
                },
            },
        )
        if status != 200:
            raise VerificationFailure("mcp_initialize")
        validate_initialize_response(body, request_id=1, protocol=protocol)
        session = response_headers.get("mcp-session-id")
        mcp_headers = dict(accept)
        if session:
            mcp_headers["Mcp-Session-Id"] = session
        with client.stream(
            "POST",
            "/mcp",
            headers=mcp_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        ) as initialized:
            if initialized.status_code not in (200, 202):
                raise VerificationFailure("mcp_initialized")
            for _ in _bounded_chunks(initialized.iter_bytes(), MAX_HTTP_JSON, "mcp_bound"):
                pass
        status, body, _ = _stream_mcp_request(
            client,
            2,
            mcp_headers,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        try:
            tools = body["result"]["tools"]
            names = {item["name"] for item in tools}
        except (KeyError, TypeError):
            raise VerificationFailure("mcp_tools") from None
        if status != 200 or body.get("jsonrpc") != "2.0" or names != EXPECTED_TOOLS:
            raise VerificationFailure("mcp_tools")


def _bounded_chunks(
    chunks: Iterable[bytes], max_bytes: int, failure_code: str
) -> Iterator[bytes]:
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > max_bytes:
            raise VerificationFailure(failure_code)
        yield chunk


def verify_e2e(base_url: str, api_key: str, timeout: float, run_id: str) -> None:
    from smoke_mineru import synthetic_pdf

    upload_key, task_key = idempotency_keys(run_id)
    headers = {"X-API-Key": api_key}
    with httpx.Client(base_url=base_url, timeout=30, follow_redirects=False) as client:
        status, upload, _ = _stream_json_request(
            client,
            "POST",
            "/v1/uploads",
            headers={
                **headers,
                "Content-Type": "application/pdf",
                "X-Document-Name": SYNTHETIC_FILENAME_CANARY,
                "Idempotency-Key": upload_key,
            },
            content=synthetic_pdf(),
        )
        if status not in (200, 201):
            raise VerificationFailure("rest_upload", EXIT_E2E)
        try:
            file_id = upload["file_id"]
        except (KeyError, TypeError):
            raise VerificationFailure("rest_upload", EXIT_E2E) from None
        status, submitted, _ = _stream_json_request(
            client,
            "POST",
            "/v1/tasks",
            headers=headers,
            json={"sources": [{"file_id": file_id}], "idempotency_key": task_key},
        )
        if status not in (200, 202):
            raise VerificationFailure("rest_submit", EXIT_E2E)
        try:
            batch_id = submitted["batch_id"]
        except (KeyError, TypeError):
            raise VerificationFailure("rest_submit", EXIT_E2E) from None
        deadline = time.monotonic() + timeout
        while True:
            status, value, _ = _stream_json_request(
                client, "GET", f"/v1/tasks/{batch_id}", headers=headers
            )
            if status != 200 or not isinstance(value, dict):
                raise VerificationFailure("rest_status", EXIT_E2E)
            if value.get("status") in (
                "completed",
                "completed_with_errors",
                "failed",
                "cancelled",
            ):
                break
            if time.monotonic() >= deadline:
                raise VerificationFailure("e2e_timeout", EXIT_E2E)
            time.sleep(2)
        artifacts = value.get("artifacts")
        if value.get("status") not in ("completed", "completed_with_errors") or not artifacts:
            raise VerificationFailure("e2e_result", EXIT_E2E)
        download_url = artifacts[0].get("download_url")
        if not isinstance(download_url, str):
            raise VerificationFailure("artifact_url", EXIT_E2E)
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
        artifact_path: Path | None = None
        try:
            with client.stream("GET", parsed.path, headers=headers) as artifact:
                if artifact.status_code != 200:
                    raise VerificationFailure("artifact_download", EXIT_E2E)
                declared = artifact.headers.get("content-length")
                if declared is not None:
                    try:
                        if int(declared) <= 0 or int(declared) > MAX_ARTIFACT:
                            raise VerificationFailure("artifact_bound", EXIT_E2E)
                    except ValueError:
                        raise VerificationFailure("artifact_bound", EXIT_E2E) from None
                artifact_path = stream_to_tempfile(artifact.iter_bytes())
            validate_result_zip(artifact_path)
        finally:
            if artifact_path is not None:
                artifact_path.unlink(missing_ok=True)


def verify_isolation(cpu_image: str, gpu_image: str) -> None:
    probe = (
        "import importlib.metadata as m,importlib.util as i,json;"
        "pkgs={d.metadata['Name'].lower() for d in m.distributions() if d.metadata['Name']};"
        "print(json.dumps({'vllm':bool(i.find_spec('vllm')),"
        "'paddlepaddle':'paddlepaddle' in pkgs,"
        "'paddlepaddle_gpu':'paddlepaddle-gpu' in pkgs}))"
    )
    cpu = json.loads(
        _run(
            [
                "docker", "run", "--rm", "--network", "none",
                cpu_image, "python", "-c", probe,
            ],
            timeout=60,
        )
    )
    gpu = json.loads(
        _run(
            [
                "docker", "run", "--rm", "--network", "none",
                gpu_image, "python", "-c", probe,
            ],
            timeout=60,
        )
    )
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
    logs = _run(
        [
            "docker",
            "compose",
            "logs",
            "--no-color",
            "--tail",
            "200",
            "ocr-production",
            "mineru-api",
            "mineru-vlm",
        ]
    )
    encoded = logs.encode()
    canaries = (
        api_key.encode(),
        SYNTHETIC_FILENAME_CANARY.encode(),
        SYNTHETIC_TEXT_CANARY.encode(),
    )
    if any(canary in encoded for canary in canaries) or any(
        len(line) > 4096 for line in logs.splitlines()
    ):
        raise VerificationFailure("log_boundary", EXIT_SAFETY)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--phase", choices=("static", "runtime", "e2e", "all"), default="all"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("models/pp-structure-v3/model-manifest.json"),
    )
    parser.add_argument("--cpu-image", default="ocr-mcp-server:ppstructure-cpu-dev")
    parser.add_argument(
        "--production-image", default=DEFAULT_IMAGES["ocr-production"]
    )
    parser.add_argument("--mineru-api-image", default=DEFAULT_IMAGES["mineru-api"])
    parser.add_argument("--mineru-vlm-image", default=DEFAULT_IMAGES["mineru-vlm"])
    parser.add_argument("--timeout-seconds", type=float, default=1200)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    expected_images = {
        "ocr-production": args.production_image,
        "mineru-api": args.mineru_api_image,
        "mineru-vlm": args.mineru_vlm_image,
    }
    api_key = os.environ.get("OCR_VERIFY_API_KEY", "")
    try:
        image_ids = candidate_image_ids(expected_images)
        validate_run_id(args.run_id, image_ids["ocr-production"])
        if args.phase in ("static", "all"):
            digest = verify_static(args.manifest, expected_images)
            print(safe_event("static", True, sha256=digest, image_count=3))
            verify_isolation(args.cpu_image, args.production_image)
            print(safe_event("isolation", True, image_count=2))
        if args.phase in ("runtime", "all"):
            if not api_key:
                raise VerificationFailure("api_key_missing", EXIT_CONFIG)
            verify_runtime(
                args.base_url, api_key, args.timeout_seconds, expected_images
            )
            print(safe_event("runtime", True, tool_count=3))
        if args.phase in ("e2e", "all"):
            if not api_key:
                raise VerificationFailure("api_key_missing", EXIT_CONFIG)
            verify_e2e(args.base_url, api_key, args.timeout_seconds, args.run_id)
            verify_logs(api_key)
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
