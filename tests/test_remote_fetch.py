from __future__ import annotations

from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from PIL import Image

from ocr_mcp_server.domain.errors import FileIntakeFailure
from ocr_mcp_server.services.file_intake import FileIntakeService
from ocr_mcp_server.services.file_storage import FileStorage
from ocr_mcp_server.services.file_validation import FileValidator
from ocr_mcp_server.services.remote_fetch import RemoteFileFetcher


GLOBAL_V4 = "8.8.8.8"
GLOBAL_V6 = "2606:4700:4700::1111"


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (3, 2), color=(7, 8, 9)).save(output, format="PNG")
    return output.getvalue()


class ObservedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.consumed = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


async def _consume(incoming) -> bytes:
    return b"".join([chunk async for chunk in incoming.content])


def _service(data_root: Path, max_bytes: int) -> FileIntakeService:
    return FileIntakeService(
        storage=FileStorage(data_root),
        validator=FileValidator(max_pages=500, max_image_pixels=1_000),
        max_files=20,
        max_file_size_bytes=max_bytes,
        max_batch_size_bytes=1024**3,
    )


@pytest.mark.asyncio
async def test_remote_file_uses_the_same_streaming_intake_validator(
    tmp_path: Path,
) -> None:
    payload = _png_bytes()
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200,
            headers={"Content-Type": "image/png", "Content-Length": str(len(payload))},
            content=payload,
        )

    resolver_calls: list[str] = []

    async def resolver(hostname: str) -> list[str]:
        resolver_calls.append(hostname)
        return [GLOBAL_V4, GLOBAL_V6]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        fetcher = RemoteFileFetcher(
            client=client,
            allowed_hosts=["files.example.com"],
            resolver=resolver,
            max_file_size_bytes=len(payload),
        )
        stored = await _service(tmp_path / "data", len(payload)).ingest_remote(
            str(uuid4()), "https://files.example.com/image.png", fetcher
        )

    assert stored.path.read_bytes() == payload
    assert stored.extension == ".png"
    assert requests == ["https://files.example.com/image.png"]
    assert resolver_calls == ["files.example.com"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://files.example.com/a.png",
        "https://user:password@files.example.com/a.png",
        "https://files.example.com/a.png#fragment",
        "https://files.example.com:444/a.png",
        "https://127.0.0.1/a.png",
        "https://other.example.com/a.png",
    ],
)
async def test_remote_fetch_rejects_unsafe_urls_before_transport(url: str) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=b"unexpected")

    async def resolver(hostname: str) -> list[str]:
        return [GLOBAL_V4]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = RemoteFileFetcher(
            client=client,
            allowed_hosts=["files.example.com"],
            resolver=resolver,
            max_file_size_bytes=100,
        )
        with pytest.raises(FileIntakeFailure) as exc_info:
            async with fetcher.fetch(url):
                pass

    assert exc_info.value.code == "remote_url_rejected"
    assert url not in str(exc_info.value)
    assert url not in repr(exc_info.value)
    assert url not in repr(vars(exc_info.value))
    assert requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses",
    [
        ["10.0.0.1"],
        ["127.0.0.1"],
        ["169.254.1.1"],
        ["240.0.0.1"],
        ["::1"],
        ["fe80::1"],
        ["fc00::1"],
        ["2001:db8::1"],
        [GLOBAL_V4, "10.0.0.1"],
        [GLOBAL_V6, "fe80::1"],
    ],
)
async def test_remote_fetch_rejects_any_non_global_dns_answer(
    addresses: list[str],
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=b"unexpected")

    async def resolver(hostname: str) -> list[str]:
        return addresses

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = RemoteFileFetcher(
            client=client,
            allowed_hosts=["files.example.com"],
            resolver=resolver,
            max_file_size_bytes=100,
        )
        with pytest.raises(FileIntakeFailure) as exc_info:
            async with fetcher.fetch("https://files.example.com/a.png"):
                pass

    assert exc_info.value.code == "remote_url_rejected"
    assert requests == 0


@pytest.mark.asyncio
async def test_remote_fetch_revalidates_dns_for_relative_and_cross_host_redirects() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/start.png":
            return httpx.Response(302, headers={"Location": "/middle.png"})
        if request.url.path == "/middle.png":
            return httpx.Response(
                307, headers={"Location": "https://cdn.example.com/final.png"}
            )
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=b"ok")

    resolved: list[str] = []

    async def resolver(hostname: str) -> list[str]:
        resolved.append(hostname)
        return [GLOBAL_V4]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = RemoteFileFetcher(
            client=client,
            allowed_hosts=["files.example.com", "cdn.example.com"],
            resolver=resolver,
            max_redirects=3,
            max_file_size_bytes=100,
        )
        async with fetcher.fetch("https://files.example.com/start.png") as incoming:
            assert incoming.display_name == "final.png"
            assert await _consume(incoming) == b"ok"

    assert resolved == ["files.example.com", "files.example.com", "cdn.example.com"]
    assert requested == [
        "https://files.example.com/start.png",
        "https://files.example.com/middle.png",
        "https://cdn.example.com/final.png",
    ]


@pytest.mark.asyncio
async def test_cross_host_redirect_is_rejected_before_requesting_new_host() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302, headers={"Location": "https://blocked.example.com/final.png"}
        )

    async def resolver(hostname: str) -> list[str]:
        return [GLOBAL_V4]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = RemoteFileFetcher(
            client=client,
            allowed_hosts=["files.example.com"],
            resolver=resolver,
            max_file_size_bytes=100,
        )
        with pytest.raises(FileIntakeFailure) as exc_info:
            async with fetcher.fetch("https://files.example.com/start.png"):
                pass

    assert exc_info.value.code == "remote_url_rejected"
    assert requested == ["https://files.example.com/start.png"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing_location", "loop", "too_many"])
async def test_remote_fetch_rejects_invalid_redirect_chains(mode: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "missing_location":
            return httpx.Response(302)
        if mode == "loop":
            return httpx.Response(302, headers={"Location": "/start.png"})
        return httpx.Response(302, headers={"Location": f"/next{request.url.path}.png"})

    async def resolver(hostname: str) -> list[str]:
        return [GLOBAL_V4]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = RemoteFileFetcher(
            client=client,
            allowed_hosts=["files.example.com"],
            resolver=resolver,
            max_redirects=1,
            max_file_size_bytes=100,
        )
        with pytest.raises(FileIntakeFailure) as exc_info:
            async with fetcher.fetch("https://files.example.com/start.png"):
                pass

    assert exc_info.value.code == "remote_url_rejected"


@pytest.mark.asyncio
async def test_content_length_over_limit_is_rejected_without_reading_body() -> None:
    body = ObservedStream([b"secret response body"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "image/png", "Content-Length": "101"},
            stream=body,
        )

    async def resolver(hostname: str) -> list[str]:
        return [GLOBAL_V4]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = RemoteFileFetcher(
            client=client,
            allowed_hosts=["files.example.com"],
            resolver=resolver,
            max_file_size_bytes=100,
        )
        with pytest.raises(FileIntakeFailure) as exc_info:
            async with fetcher.fetch("https://files.example.com/a.png"):
                pass

    assert exc_info.value.code == "file_too_large"
    assert body.consumed == 0
    assert body.closed


@pytest.mark.asyncio
async def test_stream_over_limit_stops_immediately_and_closes_response() -> None:
    body = ObservedStream([b"12345", b"6", b"secret response body"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": "image/png"}, stream=body
        )

    async def resolver(hostname: str) -> list[str]:
        return [GLOBAL_V4]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = RemoteFileFetcher(
            client=client,
            allowed_hosts=["files.example.com"],
            resolver=resolver,
            max_file_size_bytes=5,
        )
        with pytest.raises(FileIntakeFailure) as exc_info:
            async with fetcher.fetch("https://files.example.com/a.png") as incoming:
                await _consume(incoming)

    assert exc_info.value.code == "file_too_large"
    assert body.consumed == 2
    assert body.closed


@pytest.mark.asyncio
async def test_non_2xx_response_body_and_transport_error_never_enter_failure() -> None:
    secret = "recognized private response body"

    async def resolver(hostname: str) -> list[str]:
        return [GLOBAL_V4]

    for handler in (
        lambda request: httpx.Response(503, content=secret.encode()),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError(secret)),
    ):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            fetcher = RemoteFileFetcher(
                client=client,
                allowed_hosts=["files.example.com"],
                resolver=resolver,
                max_file_size_bytes=100,
            )
            with pytest.raises(FileIntakeFailure) as exc_info:
                async with fetcher.fetch("https://files.example.com/a.png"):
                    pass
        assert exc_info.value.code == "remote_fetch_failed"
        assert secret not in str(exc_info.value)
        assert secret not in repr(exc_info.value)
        assert secret not in repr(vars(exc_info.value))


@pytest.mark.asyncio
async def test_final_url_extension_content_type_and_magic_must_still_agree(
    tmp_path: Path,
) -> None:
    payload = _png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": "image/png"}, content=payload
        )

    async def resolver(hostname: str) -> list[str]:
        return [GLOBAL_V4]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = RemoteFileFetcher(
            client=client,
            allowed_hosts=["files.example.com"],
            resolver=resolver,
            max_file_size_bytes=len(payload),
        )
        with pytest.raises(FileIntakeFailure) as exc_info:
            await _service(tmp_path / "data", len(payload)).ingest_remote(
                str(uuid4()), "https://files.example.com/wrong.jpg", fetcher
            )

    assert exc_info.value.code == "file_type_mismatch"
