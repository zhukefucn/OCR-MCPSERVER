from __future__ import annotations

import importlib.util
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from starlette.datastructures import FormData, Headers, UploadFile
from starlette.responses import Response, StreamingResponse


ROOT = Path(__file__).parents[1]


def _load_proxy():
    path = ROOT / "scripts" / "mineru_fixed_api.py"
    spec = importlib.util.spec_from_file_location("mineru_fixed_api", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixed_policy_accepts_only_one_exact_backend_and_vlm_url() -> None:
    proxy = _load_proxy()

    proxy.validate_fixed_fields(
        [
            ("backend", "vlm-http-client"),
            ("server_url", "http://mineru-vlm:30000"),
            ("return_md", "true"),
        ]
    )

    for fields in (
        [],
        [("backend", "pipeline"), ("server_url", "http://mineru-vlm:30000")],
        [("backend", "vlm-http-client"), ("server_url", "http://attacker")],
        [
            ("backend", "vlm-http-client"),
            ("backend", "vlm-http-client"),
            ("server_url", "http://mineru-vlm:30000"),
        ],
    ):
        with pytest.raises(proxy.PolicyViolation):
            proxy.validate_fixed_fields(fields)


def test_proxy_has_fixed_loopback_upstream_and_bounded_content_contract() -> None:
    proxy = _load_proxy()

    assert proxy.UPSTREAM_BASE_URL == "http://127.0.0.1:8001"
    assert proxy.MAX_REQUEST_BYTES == 32 * 1024**2
    assert 0 < proxy.MAX_RESPONSE_BYTES <= 1024**3
    assert 0 < proxy.UPSTREAM_TIMEOUT_SECONDS <= 900
    assert proxy.allowed_response_headers(
        {
            "content-type": "application/zip",
            "connection": "keep-alive",
            "transfer-encoding": "chunked",
            "location": "http://attacker.invalid/",
            "x-secret": "no",
        }
    ) == {"content-type": "application/zip"}


def test_proxy_rejects_every_non_allowlisted_method_or_path() -> None:
    proxy = _load_proxy()

    for method, path in (
        ("POST", "/tasks"),
        ("GET", "/health"),
        ("GET", "/tasks/task-1"),
        ("GET", "/tasks/task-1/result"),
    ):
        proxy.validate_upstream_target(method, path)

    for method, path in (
        ("POST", "/health"),
        ("GET", "/docs"),
        ("GET", "http://attacker.invalid/"),
        ("GET", "//attacker.invalid/"),
        ("DELETE", "/tasks/task-1"),
    ):
        with pytest.raises(proxy.PolicyViolation):
            proxy.validate_upstream_target(method, path)


@pytest.mark.parametrize(
    ("source_name", "expected_name"),
    (
        ("statement.PDF", "document.pdf"),
        ("scan.png", "document.png"),
        ("photo.jpg", "document.jpg"),
        ("photo.jpeg", "document.jpeg"),
    ),
)
def test_proxy_accepts_starlette_upload_only_in_files_field_and_preserves_type(
    source_name: str, expected_name: str
) -> None:
    proxy = _load_proxy()
    upload = UploadFile(
        BytesIO(b"synthetic"),
        filename=source_name,
        headers=Headers({"content-type": "application/octet-stream"}),
    )

    field, forwarded = proxy.prepare_upload("files", upload)

    assert field == "files"
    assert forwarded[0] == expected_name
    assert forwarded[1] is upload.file


@pytest.mark.parametrize(
    ("field_name", "source_name"),
    (
        ("file", "statement.pdf"),
        ("files[]", "statement.pdf"),
        ("files", "statement.txt"),
        ("files", ""),
    ),
)
def test_proxy_rejects_wrong_upload_field_or_unsupported_suffix(
    field_name: str, source_name: str
) -> None:
    proxy = _load_proxy()
    upload = UploadFile(BytesIO(b"synthetic"), filename=source_name)

    with pytest.raises(proxy.PolicyViolation):
        proxy.prepare_upload(field_name, upload)


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_proxy_streams_upstream_response_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _load_proxy()
    stream = _ChunkStream([b"one", b"two"])
    clients: list[httpx.AsyncClient] = []
    real_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        client = real_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "application/zip"},
                    stream=stream,
                )
            ),
            **kwargs,
        )
        clients.append(client)
        return client

    monkeypatch.setattr(proxy.httpx, "AsyncClient", client_factory)
    response = await proxy._request_upstream("GET", "/tasks/task-1/result")

    assert isinstance(response, StreamingResponse)
    assert stream.yielded == 0
    body = b"".join([chunk async for chunk in response.body_iterator])
    assert body == b"onetwo"
    assert stream.closed is True
    assert clients[0].is_closed is True


@pytest.mark.asyncio
async def test_proxy_rejects_declared_oversized_response_before_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _load_proxy()
    stream = _ChunkStream([b"must-not-be-read"])
    clients: list[httpx.AsyncClient] = []
    real_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        client = real_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-length": str(proxy.MAX_RESPONSE_BYTES + 1)},
                    stream=stream,
                )
            ),
            **kwargs,
        )
        clients.append(client)
        return client

    monkeypatch.setattr(proxy.httpx, "AsyncClient", client_factory)
    with pytest.raises(HTTPException, match="upstream_response_too_large"):
        await proxy._request_upstream("GET", "/tasks/task-1/result")

    assert stream.yielded == 0
    assert stream.closed is True
    assert clients[0].is_closed is True


@pytest.mark.asyncio
async def test_proxy_stops_chunked_response_at_cumulative_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _load_proxy()
    monkeypatch.setattr(proxy, "MAX_RESPONSE_BYTES", 3)
    stream = _ChunkStream([b"ab", b"cd"])
    clients: list[httpx.AsyncClient] = []
    real_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        client = real_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=stream)
            ),
            **kwargs,
        )
        clients.append(client)
        return client

    monkeypatch.setattr(proxy.httpx, "AsyncClient", client_factory)
    response = await proxy._request_upstream("GET", "/tasks/task-1/result")
    with pytest.raises(HTTPException, match="upstream_response_too_large"):
        _ = [chunk async for chunk in response.body_iterator]

    assert stream.yielded == 2
    assert stream.closed is True
    assert clients[0].is_closed is True


@pytest.mark.asyncio
async def test_upstream_wait_fails_immediately_when_child_exits() -> None:
    proxy = _load_proxy()

    class ExitedProcess:
        @staticmethod
        def poll() -> int:
            return 17

    with pytest.raises(RuntimeError, match="mineru_upstream_exited"):
        await proxy._wait_for_upstream(ExitedProcess())


@pytest.mark.parametrize(
    "outcome",
    ("success", "upstream_error", "policy_rejection", "size_rejection"),
)
@pytest.mark.asyncio
async def test_submit_task_closes_form_upload_on_every_exit(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = _load_proxy()

    class LogicalFile(BytesIO):
        def __init__(self, logical_size: int) -> None:
            super().__init__(b"synthetic")
            self.logical_size = logical_size
            self.logical_position = 0

        def seek(self, offset: int, whence: int = 0) -> int:
            if whence == 2:
                self.logical_position = self.logical_size + offset
            elif whence == 0:
                self.logical_position = offset
            else:
                self.logical_position += offset
            return self.logical_position

        def tell(self) -> int:
            return self.logical_position

    logical_size = 30 * 1024**2 + 1 if outcome == "size_rejection" else 9
    upload = UploadFile(LogicalFile(logical_size), filename="statement.pdf")
    backend = "pipeline" if outcome == "policy_rejection" else "vlm-http-client"
    form = FormData(
        [
            ("backend", backend),
            ("server_url", "http://mineru-vlm:30000"),
            ("files", upload),
        ]
    )

    class FakeRequest:
        headers = {"content-length": "1024"}

        @staticmethod
        async def form(**kwargs: object) -> FormData:
            return form

    async def upstream(*args: object, **kwargs: object) -> Response:
        if outcome == "upstream_error":
            raise HTTPException(status_code=502, detail="upstream_unavailable")
        return Response(status_code=202)

    monkeypatch.setattr(proxy, "_request_upstream", upstream)
    if outcome == "success":
        response = await proxy.submit_task(FakeRequest())
        assert response.status_code == 202
    else:
        with pytest.raises(HTTPException):
            await proxy.submit_task(FakeRequest())

    assert upload.file.closed is True
