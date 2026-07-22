"""Policy-checked streaming HTTPS fetches for allowlisted hosts.

The transport used in production must not bypass the hostname validated here.
Application-level resolution checks cannot completely eliminate DNS rebinding;
deployments must also enforce egress-network policy as a second protection layer.
The resolver and HTTPX transport remain replaceable so a deployment can bind
them more tightly when its networking stack supports that capability.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
import asyncio
import ipaddress
from pathlib import PurePosixPath
import socket
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from ..domain.errors import FileIntakeErrorCode, FileIntakeFailure
from ..domain.files import IncomingFile


DNSResolver = Callable[[str], Awaitable[Sequence[str]]]
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


async def default_dns_resolver(hostname: str) -> Sequence[str]:
    """Resolve a hostname without blocking the event loop."""

    loop = asyncio.get_running_loop()
    results = await loop.getaddrinfo(
        hostname,
        443,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
    )
    return tuple(dict.fromkeys(result[4][0] for result in results))


class RemoteFileFetcher:
    """Fetch one remote stream after URL, DNS, redirect, and size checks."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        allowed_hosts: Sequence[str],
        max_file_size_bytes: int,
        resolver: DNSResolver = default_dns_resolver,
        max_redirects: int = 3,
        timeout_seconds: float = 30,
    ) -> None:
        self._client = client
        self._allowed_hosts = frozenset(
            self._canonical_hostname(host) for host in allowed_hosts
        )
        self._max_file_size_bytes = max_file_size_bytes
        self._resolver = resolver
        self._max_redirects = max_redirects
        self._timeout = timeout_seconds

    @asynccontextmanager
    async def fetch(self, url: str) -> AsyncIterator[IncomingFile]:
        """Yield a one-pass stream and always close its HTTP response."""

        response, final_url = await self._open_response(url)
        split = urlsplit(final_url)
        display_name = PurePosixPath(unquote(split.path)).name
        incoming = IncomingFile(
            display_name=display_name,
            declared_mime=response.headers.get("Content-Type", ""),
            content=self._response_chunks(response),
        )
        try:
            yield incoming
        finally:
            await response.aclose()

    async def _open_response(self, url: str) -> tuple[httpx.Response, str]:
        current_url = url
        visited: set[str] = set()
        redirects = 0

        while True:
            await self._validate_url_and_dns(current_url)
            cycle_key = self._cycle_key(current_url)
            if cycle_key in visited:
                raise FileIntakeFailure(FileIntakeErrorCode.REMOTE_URL_REJECTED)
            visited.add(cycle_key)

            try:
                request = self._client.build_request(
                    "GET", current_url, timeout=self._timeout
                )
                response = await self._client.send(
                    request,
                    stream=True,
                    follow_redirects=False,
                )
            except Exception:
                raise FileIntakeFailure(
                    FileIntakeErrorCode.REMOTE_FETCH_FAILED
                ) from None

            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("Location")
                await response.aclose()
                if not location or redirects >= self._max_redirects:
                    raise FileIntakeFailure(
                        FileIntakeErrorCode.REMOTE_URL_REJECTED
                    )
                current_url = urljoin(current_url, location)
                redirects += 1
                continue

            if not 200 <= response.status_code < 300:
                await response.aclose()
                raise FileIntakeFailure(FileIntakeErrorCode.REMOTE_FETCH_FAILED)

            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except (TypeError, ValueError):
                    await response.aclose()
                    raise FileIntakeFailure(
                        FileIntakeErrorCode.REMOTE_FETCH_FAILED
                    ) from None
                if declared_length < 0:
                    await response.aclose()
                    raise FileIntakeFailure(
                        FileIntakeErrorCode.REMOTE_FETCH_FAILED
                    )
                if declared_length > self._max_file_size_bytes:
                    await response.aclose()
                    raise FileIntakeFailure(FileIntakeErrorCode.TOO_LARGE)
            return response, current_url

    async def _validate_url_and_dns(self, url: str) -> None:
        try:
            split = urlsplit(url)
            if split.scheme.lower() != "https":
                raise ValueError
            if split.username is not None or split.password is not None:
                raise ValueError
            if split.fragment:
                raise ValueError
            if split.port not in (None, 443):
                raise ValueError
            if split.hostname is None:
                raise ValueError
            raw_hostname = split.hostname.rstrip(".")
            try:
                ipaddress.ip_address(raw_hostname)
            except ValueError:
                pass
            else:
                raise ValueError
            hostname = self._canonical_hostname(raw_hostname)
            if hostname not in self._allowed_hosts:
                raise ValueError
        except (UnicodeError, ValueError):
            raise FileIntakeFailure(FileIntakeErrorCode.REMOTE_URL_REJECTED) from None

        try:
            addresses = await self._resolver(hostname)
            parsed = tuple(ipaddress.ip_address(address) for address in addresses)
        except Exception:
            raise FileIntakeFailure(FileIntakeErrorCode.REMOTE_URL_REJECTED) from None
        if not parsed or any(not address.is_global for address in parsed):
            raise FileIntakeFailure(FileIntakeErrorCode.REMOTE_URL_REJECTED)

    async def _response_chunks(
        self, response: httpx.Response
    ) -> AsyncIterator[bytes]:
        total = 0
        try:
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                total += len(chunk)
                if total > self._max_file_size_bytes:
                    raise FileIntakeFailure(FileIntakeErrorCode.TOO_LARGE)
                yield chunk
        except FileIntakeFailure:
            raise
        except Exception:
            raise FileIntakeFailure(FileIntakeErrorCode.REMOTE_FETCH_FAILED) from None
        finally:
            await response.aclose()

    @staticmethod
    def _canonical_hostname(hostname: str) -> str:
        candidate = hostname.strip().rstrip(".").lower()
        if not candidate or "*" in candidate:
            raise ValueError("invalid hostname")
        return candidate.encode("idna").decode("ascii")

    @staticmethod
    def _cycle_key(url: str) -> str:
        split = urlsplit(url)
        hostname = (split.hostname or "").lower().rstrip(".")
        port = split.port or 443
        return f"https://{hostname}:{port}{split.path}?{split.query}"
