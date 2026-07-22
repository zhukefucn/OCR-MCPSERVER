from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from email.parser import BytesParser
from email.policy import default
from io import BytesIO
import json
from pathlib import Path
import stat
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
    "server_url": "https://vlm.internal:30000/",
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
        "safe/vlm/safe.md": "# redacted result",
        "safe/vlm/safe_middle.json": "{}",
        "safe/vlm/safe_content_list.json": "[]",
        "safe/vlm/safe_content_list_v2.json": "[]",
        "safe/vlm/images/page-1.png": b"image-bytes",
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
        ("safe/vlm/safe.md", "# result"),
        ("safe/vlm/safe_middle.json", "{}"),
        ("safe/vlm/safe_content_list.json", "[]"),
        ("safe/vlm/safe_content_list_v2.json", "[]"),
        ("safe/vlm/images/page.png", b"image"),
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
        if request.url.path.endswith("/results"):
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
        "result_url": "https://api.example.test/api/tasks/upstream-1/results",
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
                    "result_url": "https://api.example.test/api/tasks/upstream-1/results",
                    "file_names": ["safe.pdf"],
                    "queued_ahead": 4,
                },
            )
        if request.url.path.endswith("/results"):
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
    assert result.result_root == tmp_path / "published" / "safe"
    assert result.markdown_path == result.result_root / "vlm" / "safe.md"
    assert result.middle_json_path == result.result_root / "vlm" / "safe_middle.json"
    assert (
        result.content_list_v2_path
        == result.result_root / "vlm" / "safe_content_list_v2.json"
    )
    assert (
        result.legacy_content_list_path
        == result.result_root / "vlm" / "safe_content_list.json"
    )
    assert result.images_directory == result.result_root / "vlm" / "images"
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
        if request.url.path.endswith("/results"):
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
        if request.url.path.endswith("/results"):
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
        if request.url.path.endswith("/results"):
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
                result_url="https://user:password@api.example.test/api/tasks/upstream-1/results"
            ),
        ),
        httpx.Response(
            202,
            json=_submission_payload(
                result_url="https://api.example.test/api/tasks/upstream-1/results#secret"
            ),
        ),
        httpx.Response(
            202,
            json=_submission_payload(
                result_url="https://api.example.test/api/tasks/upstream-1/unexpected"
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
                result_url="https://api.example.test/api/tasks/任务/results",
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
        if request.url.path.endswith("/results"):
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
        if request.url.path.endswith("/results"):
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
        if request.url.path.endswith("/results"):
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
    assert not (tmp_path / "published" / "safe").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("file_type", [stat.S_IFLNK, stat.S_IFIFO])
async def test_zip_rejects_symlinks_and_other_special_file_types(
    tmp_path: Path, file_type: int
) -> None:
    special = ZipInfo("safe/vlm/special")
    special.create_system = 3
    special.external_attr = (file_type | 0o777) << 16
    archive = _zip_entry_bytes(_base_entries() + [(special, b"target")])

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, _archive_handler(archive))

    assert exc_info.value.code == "mineru_archive_unsafe"
    assert not (tmp_path / "published" / "safe").exists()


@pytest.mark.asyncio
async def test_zip_rejects_duplicate_normalized_destinations(tmp_path: Path) -> None:
    archive = _zip_entry_bytes(
        _base_entries()
        + [
            ("safe/vlm/duplicate.txt", b"first"),
            ("safe/vlm/DUPLICATE.TXT", b"second"),
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
            ("safe/vlm/safe_content_list_v2.json", "[]"),
            ("safe/vlm/large.bin", b"x" * 100),
        ]
    )

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(
            tmp_path,
            _archive_handler(archive),
            settings=_settings(max_uncompressed_bytes=50),
        )

    assert exc_info.value.code == "mineru_archive_unsafe"
    assert not (tmp_path / "published" / "safe").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entries",
    [
        [("safe/vlm/safe.md", "# no manifest")],
        [
            ("safe/vlm/safe_content_list_v2.json", "[]"),
            ("safe/other/other_content_list_v2.json", "[]"),
        ],
        [("safe/vlm/safe_content_list_v2.json", "not-json")],
        [("safe/vlm/safe_content_list_v2.json", "{}")],
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
        [("safe/vlm/wrong_content_list_v2.json", "[]")],
        [
            ("safe/vlm/safe_content_list_v2.json", "[]"),
            ("safe/other/extra.txt", "mismatched parse directory"),
        ],
        [
            ("safe/vlm/safe_content_list_v2.json", "[]"),
            ("safe/vlm/nested/images/page.png", b"escaped image layout"),
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
    existing = tmp_path / "published" / "safe"
    existing.mkdir(parents=True)
    marker = existing / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, _archive_handler(_zip_bytes()))

    assert exc_info.value.code == "mineru_archive_unsafe"
    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (existing / "vlm").exists()


@pytest.mark.asyncio
async def test_cancellation_cleans_partial_zip_and_staging_output(tmp_path: Path) -> None:
    class CancelledStream(ChunkedStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"partial zip bytes"
            raise asyncio.CancelledError

    import asyncio

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
            ("safe/vlm/images/", b""),
            ("safe/vlm/", b""),
            ("safe/", b""),
        ]
    )

    result = await _parse_with_handler(tmp_path, _archive_handler(archive))

    assert result.content_list_v2_path.is_file()


@pytest.mark.asyncio
async def test_archive_rejects_unrelated_empty_directory_layout(tmp_path: Path) -> None:
    archive = _zip_entry_bytes(_base_entries() + [("other/", b"")])

    with pytest.raises(MinerUFailure) as exc_info:
        await _parse_with_handler(tmp_path, _archive_handler(archive))

    assert exc_info.value.code == "mineru_archive_unsafe"


@pytest.mark.asyncio
async def test_document_and_parse_names_may_literally_be_images(tmp_path: Path) -> None:
    archive = _zip_entry_bytes(
        [
            ("images/images/images_content_list_v2.json", "[]"),
            ("images/images/images.md", "# valid"),
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

    assert result.result_root.name == "images"
    assert result.content_list_v2_path.is_file()
