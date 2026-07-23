"""Fixed-policy network facade for the loopback-only MinerU API."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import re
import subprocess
from typing import AsyncIterator, Iterable, Mapping
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.datastructures import UploadFile


FIXED_BACKEND = os.getenv("MINERU_FIXED_BACKEND", "vlm-http-client")
FIXED_VLM_URL = os.getenv("MINERU_FIXED_VLM_URL", "http://mineru-vlm:30000")
UPSTREAM_BASE_URL = os.getenv("MINERU_UPSTREAM_URL", "http://127.0.0.1:8001")
PUBLIC_HOST = "mineru-api:8000"
MAX_REQUEST_BYTES = 32 * 1024**2
MAX_RESPONSE_BYTES = 1024**3
UPSTREAM_TIMEOUT_SECONDS = 900
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
RESPONSE_HEADERS = {"content-type", "content-disposition"}
SUPPORTED_UPLOADS = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


class PolicyViolation(ValueError):
    pass


def validate_upstream_target(method: str, path: str) -> None:
    allowed = (
        (method == "POST" and path == "/tasks")
        or (method == "GET" and path == "/health")
        or (
            method == "GET"
            and re.fullmatch(r"/tasks/[^/?#]+(?:/result)?", path) is not None
        )
    )
    if not allowed or path.startswith("//") or "://" in path:
        raise PolicyViolation("upstream_target")


def validate_fixed_fields(fields: Iterable[tuple[str, object]]) -> None:
    values: dict[str, list[str]] = {"backend": [], "server_url": []}
    for key, value in fields:
        if key in values and isinstance(value, str):
            values[key].append(value)
    if values["backend"] != [FIXED_BACKEND]:
        raise PolicyViolation("backend_policy")
    if values["server_url"] != [FIXED_VLM_URL]:
        raise PolicyViolation("server_policy")


def allowed_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key.lower(): value
        for key, value in headers.items()
        if key.lower() in RESPONSE_HEADERS and key.lower() not in HOP_BY_HOP_HEADERS
    }


def prepare_upload(
    field_name: str, upload: UploadFile
) -> tuple[str, tuple[str, object, str]]:
    if field_name != "files":
        raise PolicyViolation("upload_field")
    filename = upload.filename or ""
    suffix = os.path.splitext(filename)[1].lower()
    content_type = SUPPORTED_UPLOADS.get(suffix)
    if content_type is None:
        raise PolicyViolation("upload_type")
    return "files", (f"document{suffix}", upload.file, content_type)


async def _bounded_response_body(
    upstream: httpx.Response, client: httpx.AsyncClient
) -> AsyncIterator[bytes]:
    total = 0
    try:
        async for chunk in upstream.aiter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise HTTPException(
                    status_code=502, detail="upstream_response_too_large"
                )
            yield chunk
    finally:
        await upstream.aclose()
        await client.aclose()


async def _request_upstream(
    method: str,
    path: str,
    *,
    data: list[tuple[str, str]] | None = None,
    files: list[tuple[str, tuple[str, object, str]]] | None = None,
) -> Response:
    try:
        validate_upstream_target(method, path)
    except PolicyViolation as exc:
        raise HTTPException(status_code=404, detail="route_not_allowed") from exc
    timeout = httpx.Timeout(UPSTREAM_TIMEOUT_SECONDS)
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    upstream: httpx.Response | None = None
    try:
        request = client.build_request(
            method,
            f"{UPSTREAM_BASE_URL}{path}",
            data=data,
            files=files,
            headers={"host": PUBLIC_HOST},
        )
        upstream = await client.send(request, stream=True)
        declared = upstream.headers.get("content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError as exc:
                raise HTTPException(
                    status_code=502, detail="invalid_upstream_content_length"
                ) from exc
            if declared_size < 0 or declared_size > MAX_RESPONSE_BYTES:
                raise HTTPException(
                    status_code=502, detail="upstream_response_too_large"
                )
        return StreamingResponse(
            _bounded_response_body(upstream, client),
            status_code=upstream.status_code,
            headers=allowed_response_headers(upstream.headers),
        )
    except HTTPException:
        if upstream is not None:
            await upstream.aclose()
        await client.aclose()
        raise
    except Exception as exc:
        if upstream is not None:
            await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail="upstream_unavailable") from exc


async def _wait_for_upstream(process: subprocess.Popen[bytes]) -> None:
    for _ in range(120):
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError("mineru_upstream_exited")
        try:
            async with httpx.AsyncClient(timeout=1) as client:
                response = await client.get(f"{UPSTREAM_BASE_URL}/health")
            if response.status_code == 200:
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)
    raise RuntimeError("mineru_upstream_unavailable")


@asynccontextmanager
async def lifespan(_: FastAPI):
    environment = os.environ.copy()
    environment["MINERU_API_DISABLE_ACCESS_LOG"] = "1"
    process = subprocess.Popen(
        ["mineru-api", "--host", "127.0.0.1", "--port", "8001"],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await _wait_for_upstream(process)
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> Response:
    return await _request_upstream("GET", "/health")


@app.post("/tasks")
async def submit_task(request: Request) -> Response:
    declared = request.headers.get("content-length")
    if declared is None:
        raise HTTPException(status_code=411, detail="content_length_required")
    try:
        declared_size = int(declared)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid_content_length") from exc
    if not 0 < declared_size <= MAX_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="request_too_large")

    form = await request.form(max_files=1, max_fields=64, max_part_size=64 * 1024)
    items = list(form.multi_items())
    try:
        validate_fixed_fields(items)
    except PolicyViolation as exc:
        raise HTTPException(status_code=400, detail="fixed_policy_violation") from exc

    data: list[tuple[str, str]] = []
    files: list[tuple[str, tuple[str, object, str]]] = []
    total_file_bytes = 0
    for key, value in items:
        if isinstance(value, UploadFile):
            value.file.seek(0, 2)
            size = value.file.tell()
            value.file.seek(0)
            if size > 30 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="file_too_large")
            total_file_bytes += size
            try:
                files.append(prepare_upload(key, value))
            except PolicyViolation as exc:
                raise HTTPException(
                    status_code=400, detail="unsupported_upload"
                ) from exc
        else:
            data.append((key, str(value)))
    if total_file_bytes > MAX_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="request_too_large")
    if len(files) != 1:
        raise HTTPException(status_code=400, detail="one_file_required")
    return await _request_upstream("POST", "/tasks", data=data, files=files)


@app.get("/tasks/{task_id}")
async def task_status(task_id: str) -> Response:
    return await _request_upstream("GET", f"/tasks/{quote(task_id, safe='')}")


@app.get("/tasks/{task_id}/result")
async def task_result(task_id: str) -> Response:
    return await _request_upstream(
        "GET", f"/tasks/{quote(task_id, safe='')}/result"
    )


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)


if __name__ == "__main__":
    main()
