from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from email.parser import BytesParser
from email.policy import default
from io import BytesIO
import json
import os
from pathlib import Path
import stat
import subprocess
import threading
import time
from typing import Any
from zipfile import ZipFile, ZipInfo

import httpx
import pytest

from ocr_mcp_server.domain import (
    MinerUDocumentResult,
    MinerUFailure,
    MinerUParseRequest,
    MinerUProgress,
    MinerUProgressStatus,
)
from ocr_mcp_server.settings import MinerUSettings


FORM_VALUES = {
    "backend": "vlm-http-client",
    "server_url": "https://vlm.internal:30000",
    "lang_list": "ch",
    "parse_method": "auto",
    "formula_enable": "true",
    "table_enable": "true",
    "image_analysis": "true",
    "return_md": "true",
    "return_middle_json": "true",
    "return_model_output": "false",
    "return_content_list": "true",
    "return_images": "true",
    "response_format_zip": "true",
    "return_original_file": "false",
    "client_side_output_generation": "false",
    "start_page_id": "0",
    "end_page_id": "99999",
}


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False
        self.consumed = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _settings(**overrides: Any) -> MinerUSettings:
    values: dict[str, Any] = {
        "api_url": "https://api.example.test/api",
        "vlm_server_url": "https://vlm.internal:30000",
        "poll_interval_seconds": 0.25,
        "retry_backoff_seconds": 0.1,
        "retry_max_backoff_seconds": 0.4,
        "task_deadline_seconds": 10,
        "max_compressed_bytes": 1024 * 1024,
        "max_uncompressed_bytes": 4 * 1024 * 1024,
        "max_archive_entries": 100,
    }
    values.update(overrides)
    return MinerUSettings(**values)


def _request(tmp_path: Path, *, name: str = "safe.pdf") -> MinerUParseRequest:
    source = tmp_path / "planted-sensitive-path" / name
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source document bytes")
    return MinerUParseRequest(
        file_task_id="local-task-42",
        source_path=source,
        upload_name=name,
        output_directory=tmp_path / "published",
    )


def _zip_bytes(
    entries: dict[str, bytes | str] | None = None,
) -> bytes:
    selected = entries or {
        "document/vlm/document.md": "# redacted result",
        "document/vlm/document_middle.json": "{}",
        "document/vlm/document_content_list.json": "[]",
        "document/vlm/document_content_list_v2.json": "[]",
        "document/vlm/images/page-1.png": b"image-bytes",
    }
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for name, content in selected.items():
            archive.writestr(name, content)
    return output.getvalue()


def _zip_entry_bytes(
    entries: list[tuple[str | ZipInfo, bytes | str]],
) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def _base_entries() -> list[tuple[str | ZipInfo, bytes | str]]:
    return [
        ("document/vlm/document.md", "# result"),
        ("document/vlm/document_middle.json", "{}"),
        ("document/vlm/document_content_list.json", "[]"),
        ("document/vlm/document_content_list_v2.json", "[]"),
        ("document/vlm/images/page.png", b"image"),
    ]


def _archive_handler(
    archive: bytes,
    *,
    content_type: str = "application/zip",
    result_headers: dict[str, str] | None = None,
    stream: httpx.AsyncByteStream | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json=_submission_payload())
        if request.url.path.endswith("/result"):
            headers = {"Content-Type": content_type}
            headers.update(result_headers or {})
            if stream is not None:
                return httpx.Response(200, headers=headers, stream=stream)
            return httpx.Response(200, headers=headers, content=archive)
        return httpx.Response(200, json={"status": "completed"})

    return handler


def _multipart_parts(request: httpx.Request) -> tuple[dict[str, str], tuple[str, bytes]]:
    message = BytesParser(policy=default).parsebytes(
        b"MIME-Version: 1.0\r\n"
        + b"Content-Type: "
        + request.headers["Content-Type"].encode("ascii")
        + b"\r\n\r\n"
        + request.content
    )
    fields: dict[str, str] = {}
    uploaded: tuple[str, bytes] | None = None
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        if filename is None:
            fields[str(name)] = part.get_content().rstrip("\r\n")
        else:
            assert name == "files"
            uploaded = (filename, part.get_payload(decode=True))
    assert uploaded is not None
    return fields, uploaded


def _submission_payload(**changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": "upstream-1",
        "status_url": "https://api.example.test/api/tasks/upstream-1",
        "result_url": "https://api.example.test/api/tasks/upstream-1/result",
        "file_names": ["safe.pdf"],
        "queued_ahead": 0,
    }
    payload.update(changes)
    return payload


async def _parse_with_handler(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    settings: MinerUSettings | None = None,
    clock: FakeClock | None = None,
) -> MinerUDocumentResult:
    from ocr_mcp_server.infra.mineru_adapter import MinerUAdapter

    selected_clock = clock or FakeClock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await MinerUAdapter(
            client=client,
            settings=settings or _settings(),
            sleep=selected_clock.sleep,
            clock=selected_clock,
        ).parse(_request(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_kind", ["sync", "async"])
async def test_valid_task_flow_forces_protocol_and_preserves_local_context(
    tmp_path: Path, callback_kind: str
) -> None:
    from ocr_mcp_server.infra.mineru_adapter import MinerUAdapter

    archive = _zip_bytes()
    stream = ChunkedStream([archive[:17], archive[17:]])
    status_payloads = [
        {"status": "pending", "queued_ahead": 3},
        {"status": "processing", "queued_ahead": 0},
        {"status": "completed"},
    ]
    submission_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submission_request
        if request.method == "POST":
            submission_request = request
            return httpx.Response(
                202,
                json={
                    "task_id": "upstream-1",
                    "status_url": "https://api.example.test/api/tasks/upstream-1",
                    "result_url": "https://api.example.test/api/tasks/upstream-1/result",
                    "file_names": ["safe.pdf"],
                    "queued_ahead": 4,
                },
            )
        if request.url.path.endswith("/result"):
            return httpx.Response(
                200,
                headers={"Content-Type": "application/zip"},
                stream=stream,
            )
        return httpx.Response(200, json=status_payloads.pop(0))

    progress: list[MinerUProgress] = []
    if callback_kind == "sync":
        callback: Callable[[MinerUProgress], object] = progress.append
    else:

        async def callback(update: MinerUProgress) -> None:
            progress.append(update)

    clock = FakeClock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await MinerUAdapter(
            client=client,
            settings=_settings(),
            sleep=clock.sleep,
            clock=clock,
        ).parse(_request(tmp_path), progress_callback=callback)

    assert isinstance(result, MinerUDocumentResult)
    assert result.file_task_id == "local-task-42"
    assert result.upstream_task_id == "upstream-1"
    assert result.result_root == tmp_path / "published" / "document"
    assert result.markdown_path == result.result_root / "vlm" / "document.md"
    assert (
        result.middle_json_path
        == result.result_root / "vlm" / "document_middle.json"
    )
    assert (
        result.content_list_v2_path
        == result.result_root / "vlm" / "document_content_list_v2.json"
    )
    assert (
        result.legacy_content_list_path
        == result.result_root / "vlm" / "document_content_list.json"
    )
    assert result.images_directory == result.result_root / "vlm" / "images"
    assert (result.images_directory / "page-1.png").read_bytes() == b"image-bytes"
    assert result.content_list_v2_path.read_text(encoding="utf-8") == "[]"
    assert [item.status for item in progress] == [
        MinerUProgressStatus.PENDING,
        MinerUProgressStatus.PROCESSING,
    ]
    assert [item.file_task_id for item in progress] == [
        "local-task-42",
        "local-task-42",
    ]
    assert [item.upstream_task_id for item in progress] == [
        "upstream-1",
        "upstream-1",
    ]
    assert [item.queued_ahead for item in progress] == [3, 0]
    assert clock.sleeps == [0.25, 0.25]
    assert stream.closed
    assert submission_request is not None
    fields, uploaded = _multipart_parts(submission_request)
    assert fields == FORM_VALUES
    assert uploaded == ("safe.pdf", b"source document bytes")


@pytest.mark.asyncio
async def test_fixed_api_canonical_document_archive_preserves_local_context(
    tmp_path: Path,
) -> None:
    from ocr_mcp_server.infra.mineru_adapter import MinerUAdapter

    local_upload_name = "2be357da-9074-4e21-a03c-f7d35d65cf10.pdf"
    archive = _zip_bytes(
        {
            "document/vlm/document.md": "# result",
            "document/vlm/document_middle.json": "{}",
            "document/vlm/document_content_list.json": "[]",
            "document/vlm/document_content_list_v2.json": "[]",
            "document/vlm/images/page-1.png": b"image-bytes",
        }
    )
    submitted_name: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submitted_name
        if request.method == "POST":
            _, uploaded = _multipart_parts(request)
            submitted_name = uploaded[0]
            return httpx.Response(202, json=_submission_payload())
        if request.url.path.endswith("/result"):
            return httpx.Response(
                200,
                headers={"Content-Type": "application/zip"},
                content=archive,
            )
        return httpx.Response(200, json={"status": "completed"})

    clock = FakeClock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await MinerUAdapter(
            client=client,
            settings=_settings(),
            sleep=clock.sleep,
            clock=clock,
        ).parse(_request(tmp_path, name=local_upload_name))

    assert submitted_name == local_upload_name
    assert result.file_task_id == "local-task-42"
    assert result.upstream_task_id == "upstream-1"
    assert result.result_root == tmp_path / "published" / "document"
    assert result.markdown_path == result.result_root / "vlm" / "document.md"
    assert (
        result.middle_json_path
        == result.result_root / "vlm" / "document_middle.json"
    )
    assert (
        result.content_list_v2_path
        == result.result_root / "vlm" / "document_content_list_v2.json"
    )
    assert (
        result.legacy_content_list_path
        == result.result_root / "vlm" / "document_content_list.json"
    )
    assert result.images_directory == result.result_root / "vlm" / "images"


@pytest.mark.asyncio
async def test_canonical_document_without_images_publishes_empty_images_directory(
    tmp_path: Path,
) -> None:
    archive = _zip_entry_bytes(
        [
            ("document/vlm/document.md", "# result"),
            ("document/vlm/document_middle.json", "{}"),
            ("document/vlm/document_content_list.json", "[]"),
            ("document/vlm/document_content_list_v2.json", "[]"),
        ]
    )

    result = await _parse_with_handler(tmp_path, _archive_handler(archive))

    assert result.images_directory == result.result_root / "vlm" / "images"
    assert result.images_directory.is_dir()
    assert list(result.images_directory.iterdir()) == []


@pytest.mark.asyncio
async def test_archive_rejects_regular_file_at_images_directory_path(
    tmp_path: Path,
) -> None:
    archive = _zip_entry_bytes(
        [
            ("document/vlm/document_content_list_v2.json", "[]"),
            ("document/vlm/document.md", "# result"),
            ("document/vlm/images", b"not-a-directory"),
        ]
    )

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, _archive_handler(archive))

    assert exc_info.value.code == "mineru_archive_unsafe"
    assert not (tmp_path / "published" / "document").exists()


@pytest.mark.parametrize("alias", ["Images", "IMAGES", "images.", "images "])
@pytest.mark.asyncio
async def test_archive_rejects_nonliteral_images_directory_alias(
    tmp_path: Path, alias: str
) -> None:
    archive = _zip_entry_bytes(
        [
            ("document/vlm/document_content_list_v2.json", "[]"),
            ("document/vlm/document.md", "# result"),
            (f"document/vlm/{alias}/page.png", b"image"),
        ]
    )

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, _archive_handler(archive))

    assert exc_info.value.code == "mineru_archive_unsafe"
    assert not (tmp_path / "published" / "document").exists()


def test_images_directory_helper_rejects_regular_file(tmp_path: Path) -> None:
    from ocr_mcp_server.infra.mineru_adapter import MinerUAdapter

    images = tmp_path / "images"
    images.write_bytes(b"not-a-directory")

    with pytest.raises(MinerUFailure) as exc_info:
        MinerUAdapter._ensure_private_images_directory(images)

    assert exc_info.value.code == "mineru_archive_unsafe"


def test_images_directory_helper_rejects_symlink(tmp_path: Path) -> None:
    from ocr_mcp_server.infra.mineru_adapter import MinerUAdapter

    target = tmp_path / "target"
    target.mkdir()
    images = tmp_path / "images"
    try:
        images.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")

    with pytest.raises(MinerUFailure) as exc_info:
        MinerUAdapter._ensure_private_images_directory(images)

    assert exc_info.value.code == "mineru_archive_unsafe"


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO regression")
def test_images_directory_helper_rejects_fifo(tmp_path: Path) -> None:
    from ocr_mcp_server.infra.mineru_adapter import MinerUAdapter

    images = tmp_path / "images"
    os.mkfifo(images)

    with pytest.raises(MinerUFailure) as exc_info:
        MinerUAdapter._ensure_private_images_directory(images)

    assert exc_info.value.code == "mineru_archive_unsafe"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_images_directory_helper_rejects_windows_junction(tmp_path: Path) -> None:
    from ocr_mcp_server.infra.mineru_adapter import MinerUAdapter

    target = tmp_path / "target"
    target.mkdir()
    images = tmp_path / "images"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(images), str(target)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("directory junctions unavailable")

    try:
        with pytest.raises(MinerUFailure) as exc_info:
            MinerUAdapter._ensure_private_images_directory(images)
        assert exc_info.value.code == "mineru_archive_unsafe"
    finally:
        if os.path.lexists(images):
            os.rmdir(images)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_url", "submitted_url"),
    [
        ("http://vlm.internal:30000", "http://vlm.internal:30000"),
        (
            "http://vlm.internal:30000/inference/",
            "http://vlm.internal:30000/inference/",
        ),
    ],
)
async def test_submission_normalizes_origin_and_preserves_non_root_vlm_path(
    tmp_path: Path, configured_url: str, submitted_url: str
) -> None:
    submission_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submission_request
        if request.method == "POST":
            submission_request = request
            return httpx.Response(202, json=_submission_payload())
        if request.url.path.endswith("/result"):
            return httpx.Response(
                200,
                headers={"Content-Type": "application/zip"},
                content=_zip_bytes(),
            )
        return httpx.Response(200, json={"status": "completed"})

    settings = MinerUSettings(
        api_url="https://api.example.test/api",
        vlm_server_url=configured_url,
    )
    await _parse_with_handler(tmp_path, handler, settings=settings)

    assert submission_request is not None
    assert submission_request.url == "https://api.example.test/api/tasks"
    fields, _ = _multipart_parts(submission_request)
    assert fields["server_url"] == submitted_url


@pytest.mark.asyncio
async def test_connection_establishment_failures_retry_before_one_accepted_submission(
    tmp_path: Path,
) -> None:
    calls = {"post": 0, "accepted": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            calls["post"] += 1
            if calls["post"] < 3:
                raise httpx.ConnectError("secret configured URL", request=request)
            calls["accepted"] += 1
            return httpx.Response(202, json=_submission_payload())
        if request.url.path.endswith("/result"):
            return httpx.Response(
                200,
                headers={"Content-Type": "application/zip"},
                content=_zip_bytes(),
            )
        return httpx.Response(200, json={"status": "completed"})

    clock = FakeClock()
    result = await _parse_with_handler(tmp_path, handler, clock=clock)

    assert result.upstream_task_id == "upstream-1"
    assert calls == {"post": 3, "accepted": 1}
    assert clock.sleeps == [0.1, 0.2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_factory",
    [
        lambda request: httpx.ReadTimeout("secret response", request=request),
        lambda request: httpx.WriteTimeout("secret filename", request=request),
        lambda request: httpx.RemoteProtocolError("secret URL", request=request),
    ],
)
async def test_ambiguous_submission_failures_are_never_retried(
    tmp_path: Path,
    failure_factory: Callable[[httpx.Request], httpx.RequestError],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise failure_factory(request)

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, handler)

    assert calls == 1
    assert exc_info.value.code == "mineru_submission_ambiguous"
    assert exc_info.value.retry_file_task_safe is False


@pytest.mark.asyncio
async def test_exhausted_connection_submission_retry_is_safe_for_whole_file_retry(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("secret host", request=request)

    clock = FakeClock()
    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(
            tmp_path,
            handler,
            clock=clock,
            settings=_settings(retry_attempts=2),
        )

    assert calls == 3
    assert clock.sleeps == [0.1, 0.2]
    assert exc_info.value.code == "mineru_unavailable"
    assert exc_info.value.retry_file_task_safe is True


@pytest.mark.asyncio
async def test_polling_retries_transient_status_and_read_failure(
    tmp_path: Path,
) -> None:
    poll_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_calls
        if request.method == "POST":
            return httpx.Response(202, json=_submission_payload())
        if request.url.path.endswith("/result"):
            return httpx.Response(
                200,
                headers={"Content-Type": "application/zip"},
                content=_zip_bytes(),
            )
        poll_calls += 1
        if poll_calls == 1:
            return httpx.Response(503, content=b"secret response body")
        if poll_calls == 2:
            raise httpx.ReadTimeout("secret text", request=request)
        return httpx.Response(200, json={"status": "completed"})

    clock = FakeClock()
    result = await _parse_with_handler(tmp_path, handler, clock=clock)

    assert result.file_task_id == "local-task-42"
    assert poll_calls == 3
    assert clock.sleeps == [0.1, 0.2]


@pytest.mark.asyncio
async def test_terminal_failed_task_is_not_retried_or_downloaded(tmp_path: Path) -> None:
    calls = {"poll": 0, "result": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json=_submission_payload())
        if request.url.path.endswith("/result"):
            calls["result"] += 1
            return httpx.Response(200, content=b"unexpected")
        calls["poll"] += 1
        return httpx.Response(
            200,
            json={"status": "failed", "error": "recognized document text"},
        )

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, handler)

    assert exc_info.value.code == "mineru_upstream_failed"
    assert calls == {"poll": 1, "result": 0}


@pytest.mark.asyncio
async def test_overall_task_deadline_stops_pending_poll_loop(tmp_path: Path) -> None:
    poll_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_calls
        if request.method == "POST":
            return httpx.Response(202, json=_submission_payload())
        poll_calls += 1
        return httpx.Response(200, json={"status": "pending"})

    clock = FakeClock()
    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(
            tmp_path,
            handler,
            clock=clock,
            settings=_settings(task_deadline_seconds=0.5),
        )

    assert exc_info.value.code == "mineru_deadline_exceeded"
    assert exc_info.value.retry_file_task_safe is False
    assert poll_calls == 2


@pytest.mark.asyncio
async def test_pending_sleep_is_capped_to_remaining_task_deadline(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json=_submission_payload())
        return httpx.Response(200, json={"status": "pending"})

    clock = FakeClock()
    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(
            tmp_path,
            handler,
            clock=clock,
            settings=_settings(task_deadline_seconds=0.1),
        )

    assert exc_info.value.code == "mineru_deadline_exceeded"
    assert clock.sleeps == [0.1]
    assert clock.value == pytest.approx(0.1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "submission_response",
    [
        httpx.Response(202, content=b"not json"),
        httpx.Response(202, json=[]),
        httpx.Response(202, json=_submission_payload(task_id="bad/task")),
        httpx.Response(
            202,
            json=_submission_payload(
                status_url="https://attacker.invalid/tasks/upstream-1"
            ),
        ),
        httpx.Response(
            202,
            json=_submission_payload(
                result_url="https://user:password@api.example.test/api/tasks/upstream-1/result"
            ),
        ),
        httpx.Response(
            202,
            json=_submission_payload(
                result_url="https://api.example.test/api/tasks/upstream-1/result#secret"
            ),
        ),
        httpx.Response(
            202,
            json=_submission_payload(
                result_url="https://api.example.test/api/tasks/upstream-1/unexpected"
            ),
        ),
        httpx.Response(
            202,
            json=_submission_payload(
                result_url="https://api.example.test/api/tasks/upstream-1/results"
            ),
        ),
    ],
)
async def test_malformed_or_untrusted_submission_is_ambiguous_and_not_followed(
    tmp_path: Path, submission_response: httpx.Response
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return submission_response

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, handler)

    assert calls == 1
    assert exc_info.value.code == "mineru_submission_ambiguous"
    assert exc_info.value.retry_file_task_safe is False


@pytest.mark.asyncio
async def test_real_singular_result_endpoint_is_accepted(tmp_path: Path) -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(
                202,
                json=_submission_payload(
                    result_url="https://api.example.test/api/tasks/upstream-1/result"
                ),
            )
        if request.url.path.endswith("/result"):
            return httpx.Response(
                200,
                headers={"Content-Type": "application/zip"},
                content=_zip_bytes(),
            )
        return httpx.Response(200, json={"status": "completed"})

    result = await _parse_with_handler(tmp_path, handler)

    assert result.content_list_v2_path.is_file()
    assert requested_paths[-1] == "/api/tasks/upstream-1/result"


@pytest.mark.asyncio
async def test_non_ascii_upstream_task_id_is_rejected_before_followup(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            202,
            json=_submission_payload(
                task_id="任务",
                status_url="https://api.example.test/api/tasks/任务",
                result_url="https://api.example.test/api/tasks/任务/result",
            ),
        )

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, handler)

    assert calls == 1
    assert exc_info.value.code == "mineru_submission_ambiguous"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_response",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"status": "unknown"}),
        httpx.Response(200, json={"status": "pending", "queued_ahead": True}),
    ],
)
async def test_malformed_status_payload_is_rejected(
    tmp_path: Path, status_response: httpx.Response
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json=_submission_payload())
        return status_response

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, handler)

    assert exc_info.value.code == "mineru_response_invalid"


@pytest.mark.asyncio
async def test_result_requires_zip_content_type(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json=_submission_payload())
        if request.url.path.endswith("/result"):
            return httpx.Response(
                200,
                headers={"Content-Type": "application/octet-stream"},
                content=_zip_bytes(),
            )
        return httpx.Response(200, json={"status": "completed"})

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, handler)

    assert exc_info.value.code == "mineru_response_invalid"


@pytest.mark.asyncio
async def test_safe_failure_discards_urls_paths_bodies_and_document_text(
    tmp_path: Path,
) -> None:
    secrets = [
        "planted-sensitive-path",
        "safe.pdf",
        "https://api.example.test/api",
        "recognized private document text",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json=_submission_payload())
        return httpx.Response(503, content=secrets[-1].encode())

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(
            tmp_path,
            handler,
            settings=_settings(retry_attempts=0),
        )

    rendered = " ".join(
        [str(exc_info.value), repr(exc_info.value), repr(vars(exc_info.value))]
    )
    assert all(secret not in rendered for secret in secrets)
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_download_retries_transient_status_and_midstream_read_error(
    tmp_path: Path,
) -> None:
    archive = _zip_bytes()

    class BrokenStream(ChunkedStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield archive[:10]
            raise httpx.ReadError("secret response content")

    result_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal result_calls
        if request.method == "POST":
            return httpx.Response(202, json=_submission_payload())
        if request.url.path.endswith("/result"):
            result_calls += 1
            if result_calls == 1:
                return httpx.Response(503, content=b"secret body")
            if result_calls == 2:
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/zip"},
                    stream=BrokenStream([]),
                )
            return httpx.Response(
                200,
                headers={"Content-Type": "application/zip"},
                content=archive,
            )
        return httpx.Response(200, json={"status": "completed"})

    clock = FakeClock()
    result = await _parse_with_handler(tmp_path, handler, clock=clock)

    assert result.content_list_v2_path.is_file()
    assert result_calls == 3
    assert clock.sleeps == [0.1, 0.2]


@pytest.mark.asyncio
async def test_download_write_failure_is_not_retried_or_safe_for_whole_file_retry(
    tmp_path: Path,
) -> None:
    result_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal result_calls
        if request.method == "POST":
            return httpx.Response(202, json=_submission_payload())
        if request.url.path.endswith("/result"):
            result_calls += 1
            raise httpx.WriteTimeout("secret URL", request=request)
        return httpx.Response(200, json={"status": "completed"})

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, handler)

    assert result_calls == 1
    assert exc_info.value.code == "mineru_unavailable"
    assert exc_info.value.retry_file_task_safe is False


@pytest.mark.asyncio
async def test_compressed_ceiling_rejects_declared_size_before_reading(
    tmp_path: Path,
) -> None:
    stream = ChunkedStream([b"secret archive body"])
    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(
            tmp_path,
            _archive_handler(
                b"",
                result_headers={"Content-Length": "101"},
                stream=stream,
            ),
            settings=_settings(max_compressed_bytes=100),
        )

    assert exc_info.value.code == "mineru_archive_unsafe"
    assert stream.consumed == 0
    assert stream.closed
    assert not list((tmp_path / "published").glob(".mineru-staging-*"))


@pytest.mark.asyncio
async def test_compressed_ceiling_stops_stream_and_removes_partial_archive(
    tmp_path: Path,
) -> None:
    stream = ChunkedStream([b"a" * 60, b"b" * 41, b"secret trailing body"])
    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(
            tmp_path,
            _archive_handler(b"", stream=stream),
            settings=_settings(max_compressed_bytes=100),
        )

    assert exc_info.value.code == "mineru_archive_unsafe"
    assert stream.consumed == 2
    assert stream.closed
    assert list((tmp_path / "published").iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_name",
    [
        "../outside.txt",
        "/absolute.txt",
        "safe\\vlm\\backslash.txt",
        "C:/drive.txt",
        "C:\\drive.txt",
        "//server/share/unc.txt",
        "\\\\server\\share\\unc.txt",
    ],
)
async def test_zip_rejects_traversal_backslash_drive_and_unc_paths(
    tmp_path: Path, unsafe_name: str
) -> None:
    archive = _zip_entry_bytes(_base_entries() + [(unsafe_name, b"secret")])
    if "\\" in unsafe_name:
        archive = archive.replace(
            unsafe_name.replace("\\", "/").encode(), unsafe_name.encode()
        )

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, _archive_handler(archive))

    assert exc_info.value.code == "mineru_archive_unsafe"
    assert not (tmp_path / "outside.txt").exists()
    assert not (tmp_path / "published" / "document").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("file_type", [stat.S_IFLNK, stat.S_IFIFO])
async def test_zip_rejects_symlinks_and_other_special_file_types(
    tmp_path: Path, file_type: int
) -> None:
    special = ZipInfo("document/vlm/special")
    special.create_system = 3
    special.external_attr = (file_type | 0o777) << 16
    archive = _zip_entry_bytes(_base_entries() + [(special, b"target")])

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, _archive_handler(archive))

    assert exc_info.value.code == "mineru_archive_unsafe"
    assert not (tmp_path / "published" / "document").exists()


@pytest.mark.asyncio
async def test_zip_rejects_duplicate_normalized_destinations(tmp_path: Path) -> None:
    archive = _zip_entry_bytes(
        _base_entries()
        + [
            ("document/vlm/duplicate.txt", b"first"),
            ("document/vlm/DUPLICATE.TXT", b"second"),
        ]
    )

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, _archive_handler(archive))

    assert exc_info.value.code == "mineru_archive_unsafe"


@pytest.mark.asyncio
async def test_zip_rejects_excessive_entry_count(tmp_path: Path) -> None:
    archive = _zip_entry_bytes(_base_entries())

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(
            tmp_path,
            _archive_handler(archive),
            settings=_settings(max_archive_entries=4),
        )

    assert exc_info.value.code == "mineru_archive_unsafe"


@pytest.mark.asyncio
async def test_zip_rejects_declared_or_actual_uncompressed_expansion(
    tmp_path: Path,
) -> None:
    archive = _zip_entry_bytes(
        [
            ("document/vlm/document_content_list_v2.json", "[]"),
            ("document/vlm/large.bin", b"x" * 100),
        ]
    )

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(
            tmp_path,
            _archive_handler(archive),
            settings=_settings(max_uncompressed_bytes=50),
        )

    assert exc_info.value.code == "mineru_archive_unsafe"
    assert not (tmp_path / "published" / "document").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entries",
    [
        [("document/vlm/document.md", "# no manifest")],
        [
            ("document/vlm/document_content_list_v2.json", "[]"),
            ("document/other/other_content_list_v2.json", "[]"),
        ],
        [("document/vlm/document_content_list_v2.json", "not-json")],
        [("document/vlm/document_content_list_v2.json", "{}")],
    ],
)
async def test_zip_rejects_missing_multiple_or_invalid_v2_manifest(
    tmp_path: Path, entries: list[tuple[str | ZipInfo, bytes | str]]
) -> None:
    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(
            tmp_path, _archive_handler(_zip_entry_bytes(entries))
        )

    assert exc_info.value.code == "mineru_archive_unsafe"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entries",
    [
        [("other/vlm/other_content_list_v2.json", "[]")],
        [("document/vlm/wrong_content_list_v2.json", "[]")],
        [
            ("document/vlm/document_content_list_v2.json", "[]"),
            ("document/other/extra.txt", "mismatched parse directory"),
        ],
        [
            ("document/vlm/document_content_list_v2.json", "[]"),
            ("document/vlm/nested/images/page.png", b"escaped image layout"),
        ],
    ],
)
async def test_zip_rejects_mismatched_document_and_image_layouts(
    tmp_path: Path, entries: list[tuple[str | ZipInfo, bytes | str]]
) -> None:
    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(
            tmp_path, _archive_handler(_zip_entry_bytes(entries))
        )

    assert exc_info.value.code == "mineru_archive_unsafe"


@pytest.mark.asyncio
async def test_existing_published_result_is_never_overwritten(tmp_path: Path) -> None:
    existing = tmp_path / "published" / "document"
    existing.mkdir(parents=True)
    marker = existing / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, _archive_handler(_zip_bytes()))

    assert exc_info.value.code == "mineru_archive_unsafe"
    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (existing / "vlm").exists()


@pytest.mark.asyncio
async def test_destination_created_at_publication_boundary_is_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ocr_mcp_server.infra import mineru_adapter as adapter_module
    from ocr_mcp_server.infra.mineru_archive import publish_directory_no_replace

    boundary_reached = False

    def create_destination_then_publish(source: Path, target: Path) -> None:
        nonlocal boundary_reached
        boundary_reached = True
        target.mkdir()
        publish_directory_no_replace(source, target)

    monkeypatch.setattr(
        adapter_module,
        "publish_directory_no_replace",
        create_destination_then_publish,
        raising=False,
    )

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, _archive_handler(_zip_bytes()))

    assert boundary_reached
    assert exc_info.value.code == "mineru_archive_unsafe"
    destination = tmp_path / "published" / "document"
    assert destination.is_dir()
    assert list(destination.iterdir()) == []
    assert not list((tmp_path / "published").glob(".mineru-staging-*"))


@pytest.mark.asyncio
async def test_cancellation_cleans_partial_zip_and_staging_output(tmp_path: Path) -> None:
    class CancelledStream(ChunkedStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"partial zip bytes"
            raise asyncio.CancelledError

    stream = CancelledStream([])
    with pytest.raises(asyncio.CancelledError):
        await _parse_with_handler(
            tmp_path, _archive_handler(b"", stream=stream)
        )

    output = tmp_path / "published"
    assert stream.closed
    assert output.is_dir()
    assert list(output.iterdir()) == []


@pytest.mark.asyncio
async def test_cancellation_during_extraction_is_responsive_and_waits_for_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zipfile

    entered_read = threading.Event()
    release_read = threading.Event()
    heartbeat = threading.Event()
    responsive_before_release: list[bool] = []
    completed_before_release: list[bool] = []
    read_calls = 0
    original_read = zipfile.ZipExtFile.read

    def controlled_read(self, size: int = -1) -> bytes:
        nonlocal read_calls
        read_calls += 1
        if read_calls == 1:
            entered_read.set()
            if not release_read.wait(timeout=2):
                raise AssertionError("test extraction release timed out")
        return original_read(self, size)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", controlled_read)
    parse_task = asyncio.create_task(
        _parse_with_handler(tmp_path, _archive_handler(_zip_bytes()))
    )
    loop = asyncio.get_running_loop()

    def coordinate_cancellation() -> None:
        if not entered_read.wait(timeout=2):
            return
        loop.call_soon_threadsafe(heartbeat.set)
        loop.call_soon_threadsafe(parse_task.cancel)
        time.sleep(0.03)
        loop.call_soon_threadsafe(parse_task.cancel)
        time.sleep(0.07)
        responsive_before_release.append(heartbeat.is_set())
        completed_before_release.append(parse_task.done())
        release_read.set()

    coordinator = threading.Thread(target=coordinate_cancellation, daemon=True)
    coordinator.start()
    try:
        with pytest.raises(asyncio.CancelledError):
            await parse_task
    finally:
        release_read.set()
        coordinator.join(timeout=2)

    assert responsive_before_release == [True]
    assert completed_before_release == [False]
    assert read_calls == 1
    output = tmp_path / "published"
    assert output.is_dir()
    assert list(output.iterdir()) == []


@pytest.mark.asyncio
async def test_download_stream_respects_overall_task_deadline(tmp_path: Path) -> None:
    archive = _zip_bytes()
    clock = FakeClock()

    class AdvancingStream(ChunkedStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield archive[:10]
            clock.value += 0.6
            yield archive[10:]

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(
            tmp_path,
            _archive_handler(b"", stream=AdvancingStream([])),
            clock=clock,
            settings=_settings(task_deadline_seconds=0.5),
        )

    assert exc_info.value.code == "mineru_deadline_exceeded"
    assert list((tmp_path / "published").iterdir()) == []


@pytest.mark.asyncio
async def test_valid_archive_allows_directory_records_after_file_records(
    tmp_path: Path,
) -> None:
    archive = _zip_entry_bytes(
        _base_entries()
        + [
            ("document/vlm/images/", b""),
            ("document/vlm/", b""),
            ("document/", b""),
        ]
    )

    result = await _parse_with_handler(tmp_path, _archive_handler(archive))

    assert result.content_list_v2_path.is_file()
    assert result.images_directory.is_dir()
    assert (result.images_directory / "page.png").read_bytes() == b"image"


@pytest.mark.asyncio
async def test_archive_rejects_unrelated_empty_directory_layout(tmp_path: Path) -> None:
    archive = _zip_entry_bytes(_base_entries() + [("other/", b"")])

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, _archive_handler(archive))

    assert exc_info.value.code == "mineru_archive_unsafe"


@pytest.mark.asyncio
async def test_parse_directory_may_literally_be_images(tmp_path: Path) -> None:
    archive = _zip_entry_bytes(
        [
            ("document/images/document_content_list_v2.json", "[]"),
            ("document/images/document.md", "# valid"),
        ]
    )
    request = _request(tmp_path, name="images.pdf")
    clock = FakeClock()
    from ocr_mcp_server.infra.mineru_adapter import MinerUAdapter

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_archive_handler(archive))
    ) as client:
        result = await MinerUAdapter(
            client=client,
            settings=_settings(),
            sleep=clock.sleep,
            clock=clock,
        ).parse(request)

    assert result.result_root.name == "document"
    assert result.content_list_v2_path.is_file()
