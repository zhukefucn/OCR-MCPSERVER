"""Bounded, content-free MinerU API/VLM integration smoke."""

from __future__ import annotations

import argparse
import asyncio
from importlib import import_module
from importlib.metadata import version
from io import BytesIO
import json
import os
import subprocess
import sys
import time
from typing import Callable, Protocol
from urllib.parse import quote, urlsplit
from zipfile import BadZipFile, ZipFile

import httpx
from packaging.version import InvalidVersion, Version


EXPECTED_MINERU_VERSION = "3.2.0"
EXPECTED_VLLM_VERSION = "0.11.2"
MAX_RESPONSE_BYTES = 1024**3
MAX_ZIP_MEMBERS = 100
MAX_MARKDOWN_BYTES = 1024**2
TASK_TIMEOUT_SECONDS = 900


class SmokeFailure(RuntimeError):
    pass


def matches_pinned_version(actual: str, expected: str) -> bool:
    """Accept an exact release with an image-specific local build suffix."""
    try:
        parsed = Version(actual)
        return Version(parsed.public) == Version(expected)
    except InvalidVersion:
        return False


class CommandResult(Protocol):
    returncode: int
    stdout: str


def resolve_cjk_font(
    *,
    command_runner: Callable[..., CommandResult] = subprocess.run,
) -> str:
    result = command_runner(
        [
            "fc-match",
            "-f",
            "%{family}\t%{file}\n",
            "Noto Sans CJK SC",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
        check=False,
    )
    line = result.stdout.splitlines()[0] if result.stdout else ""
    family, separator, font_path = line.partition("\t")
    normalized_family = family.casefold()
    if (
        result.returncode != 0
        or not separator
        or "noto" not in normalized_family
        or "cjk" not in normalized_family
        or not os.path.isabs(font_path)
    ):
        raise SmokeFailure("cjk_font")
    return font_path


def check_runtime_dependencies(
    *,
    import_module: Callable[[str], object] = import_module,
    font_match: Callable[[], str] = resolve_cjk_font,
) -> dict[str, str]:
    try:
        cv2 = import_module("cv2")
        image_module = import_module("PIL.Image")
        image_draw_module = import_module("PIL.ImageDraw")
        image_font_module = import_module("PIL.ImageFont")
        if not getattr(cv2, "__version__", None):
            raise SmokeFailure("opencv")
        font = image_font_module.truetype(font_match(), 24)
        image = image_module.new("L", (48, 48), 0)
        image_draw_module.Draw(image).text(
            (4, 4),
            "\u4e2d",
            fill=255,
            font=font,
        )
        if image.getbbox() is None:
            raise SmokeFailure("cjk_render")
        output = BytesIO()
        image.save(output, format="PNG")
        if not output.getvalue():
            raise SmokeFailure("cjk_render")
    except SmokeFailure:
        raise
    except Exception as exc:
        raise SmokeFailure("runtime_dependencies") from exc
    return {
        "opencv": "available",
        "pillow": "available",
        "cjk_font": "available",
    }


def synthetic_pdf() -> bytes:
    page_stream = (
        b"BT /F1 12 Tf 72 720 Td (Synthetic verification document) Tj ET\n"
    )
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(page_stream)).encode()
        + b" >>\nstream\n"
        + page_stream
        + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    )
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode())
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return bytes(output)


def validate_task_urls(
    *,
    api_base_url: str,
    task_id: str,
    status_url: str,
    result_url: str,
) -> tuple[str, str]:
    base = api_base_url.rstrip("/")
    parsed_base = urlsplit(base)
    if (
        parsed_base.scheme not in {"http", "https"}
        or not parsed_base.netloc
        or parsed_base.query
        or parsed_base.fragment
    ):
        raise SmokeFailure("task_submit")
    encoded_task_id = quote(task_id, safe="")
    expected_status = f"{base}/tasks/{encoded_task_id}"
    expected_result = f"{expected_status}/result"
    if status_url != expected_status or result_url != expected_result:
        raise SmokeFailure("task_submit")
    return expected_status, expected_result


def validate_result_zip(content: bytes) -> None:
    if len(content) > MAX_RESPONSE_BYTES:
        raise SmokeFailure("task_result")
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_MEMBERS:
                raise SmokeFailure("task_result")
            markdown_entries = [
                entry
                for entry in entries
                if not entry.is_dir() and entry.filename.lower().endswith(".md")
            ]
            if not markdown_entries:
                raise SmokeFailure("task_result")
            found_content = False
            for entry in markdown_entries:
                if entry.file_size > MAX_MARKDOWN_BYTES:
                    raise SmokeFailure("task_result")
                with archive.open(entry) as source:
                    markdown = source.read(MAX_MARKDOWN_BYTES + 1)
                if len(markdown) > MAX_MARKDOWN_BYTES:
                    raise SmokeFailure("task_result")
                found_content = found_content or bool(markdown.strip())
            if not found_content:
                raise SmokeFailure("task_result")
    except (BadZipFile, OSError) as exc:
        raise SmokeFailure("task_result") from exc


def _default_cuda_capability() -> tuple[int, int]:
    import torch

    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise SmokeFailure("cuda_capability")
    return tuple(torch.cuda.get_device_capability(0))


async def run_smoke(
    *,
    api_base_url: str,
    vlm_base_url: str,
    package_version: Callable[[str], str] = version,
    cuda_capability: Callable[[], tuple[int, int]] = _default_cuda_capability,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, str | int]:
    if package_version("mineru") != EXPECTED_MINERU_VERSION:
        raise SmokeFailure("mineru_version")
    if not matches_pinned_version(package_version("vllm"), EXPECTED_VLLM_VERSION):
        raise SmokeFailure("vllm_version")
    capability = cuda_capability()
    if capability != (12, 0):
        raise SmokeFailure("cuda_capability")

    timeout = httpx.Timeout(30, read=TASK_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(
        timeout=timeout,
        transport=transport,
        follow_redirects=False,
    ) as client:
        api_health = await client.get(f"{api_base_url}/health")
        if (
            api_health.status_code != 200
            or api_health.json().get("status") != "healthy"
        ):
            raise SmokeFailure("api_health")
        vlm_health = await client.get(f"{vlm_base_url}/health")
        if vlm_health.status_code != 200:
            raise SmokeFailure("vlm_health")
        models = await client.get(f"{vlm_base_url}/v1/models")
        model_payload = models.json() if models.status_code == 200 else None
        if (
            not isinstance(model_payload, dict)
            or not isinstance(model_payload.get("data"), list)
            or not model_payload["data"]
        ):
            raise SmokeFailure("vlm_models")

        response = await client.post(
            f"{api_base_url}/tasks",
            data={
                "backend": "vlm-http-client",
                "server_url": "http://mineru-vlm:30000",
                "return_md": "true",
                "response_format_zip": "true",
                "return_images": "false",
            },
            files={"files": ("synthetic.pdf", synthetic_pdf(), "application/pdf")},
        )
        payload = response.json() if response.status_code == 202 else None
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("task_id"), str)
            or not isinstance(payload.get("status_url"), str)
            or not isinstance(payload.get("result_url"), str)
        ):
            raise SmokeFailure("task_submit")
        status_url, result_url = validate_task_urls(
            api_base_url=api_base_url,
            task_id=payload["task_id"],
            status_url=payload["status_url"],
            result_url=payload["result_url"],
        )

        deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
        while True:
            status_response = await client.get(status_url)
            status_payload = (
                status_response.json() if status_response.status_code == 200 else None
            )
            status = (
                status_payload.get("status")
                if isinstance(status_payload, dict)
                else None
            )
            if status == "completed":
                break
            if status == "failed" or time.monotonic() >= deadline:
                raise SmokeFailure("task_status")
            await asyncio.sleep(2)

        result = await client.get(result_url)
        if (
            result.status_code != 200
            or len(result.content) > MAX_RESPONSE_BYTES
            or "zip" not in result.headers.get("content-type", "").lower()
        ):
            raise SmokeFailure("task_result")
        validate_result_zip(result.content)

    return {
        "mineru_version": EXPECTED_MINERU_VERSION,
        "vllm_version": EXPECTED_VLLM_VERSION,
        "cuda_capability": "12.0",
        "result_count": 1,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://mineru-api:8000")
    parser.add_argument("--vlm-url", default="http://mineru-vlm:30000")
    parser.add_argument("--runtime-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        runtime_summary = check_runtime_dependencies()
        summary = (
            runtime_summary
            if args.runtime_only
            else asyncio.run(
                run_smoke(api_base_url=args.api_url, vlm_base_url=args.vlm_url)
            )
        )
    except Exception:
        print("mineru_smoke_failed", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
