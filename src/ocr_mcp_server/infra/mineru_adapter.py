"""Asynchronous HTTP adapter for the MinerU task API."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import inspect
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from typing import Any

import httpx

from ..domain import (
    MinerUDocumentResult,
    MinerUErrorCode,
    MinerUFailure,
    MinerUParseRequest,
    MinerUProgress,
    MinerUProgressStatus,
    MinerUSubmission,
)
from ..settings import MinerUSettings
from .mineru_archive import (
    ArchiveWorkCancelled,
    ExtractedArchive,
    extract_archive,
    publish_directory_no_replace,
)


ProgressCallback = Callable[[MinerUProgress], Awaitable[None] | None]
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


_SUBMISSION_FIELDS = {
    "backend": "vlm-http-client",
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


class MinerUAdapter:
    """Submit, observe, and publish one MinerU file task."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        settings: MinerUSettings,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self._client = client
        self._settings = settings
        self._sleep = sleep
        self._clock = clock
        self._api_url = httpx.URL(str(settings.api_url))
        self._tasks_url = self._with_api_path("tasks")
        self._timeout = httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.read_timeout_seconds,
            write=settings.write_timeout_seconds,
            pool=settings.pool_timeout_seconds,
        )

    async def parse(
        self,
        request: MinerUParseRequest,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> MinerUDocumentResult:
        """Run one file task and return only a fully published result."""

        failure: MinerUFailure | None = None
        try:
            submission = await self._submit(request)
            deadline = self._clock() + self._settings.task_deadline_seconds
            await self._poll(submission, deadline, progress_callback)
            return await self._download_and_publish(request, submission, deadline)
        except asyncio.CancelledError:
            raise
        except MinerUFailure as exc:
            failure = exc
        except Exception as exc:
            failure = MinerUFailure(MinerUErrorCode.INVALID_RESPONSE, cause=exc)
        failure.__context__ = None
        failure.__cause__ = None
        failure.__suppress_context__ = True
        raise failure from None

    def _with_api_path(self, suffix: str) -> str:
        prefix = self._api_url.path.rstrip("/")
        return str(
            self._api_url.copy_with(
                path=f"{prefix}/{suffix}", query=None, fragment=None
            )
        )

    def _vlm_server_url(self) -> str:
        configured = self._settings.vlm_server_url
        serialized = str(configured)
        if (
            configured.path == "/"
            and configured.query is None
            and configured.fragment is None
        ):
            if not serialized.endswith("/"):
                raise ValueError("origin URL serialization must end with a root path")
            return serialized[:-1]
        return serialized

    async def _submit(self, request: MinerUParseRequest) -> MinerUSubmission:
        source = request.source_path.read_bytes()
        fields = dict(_SUBMISSION_FIELDS)
        fields["server_url"] = self._vlm_server_url()
        response: httpx.Response | None = None
        for attempt in range(self._settings.retry_attempts + 1):
            try:
                response = await self._client.post(
                    self._tasks_url,
                    data=fields,
                    files={"files": (request.upload_name, source)},
                    timeout=self._timeout,
                )
                break
            except asyncio.CancelledError:
                raise
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt >= self._settings.retry_attempts:
                    raise MinerUFailure(
                        MinerUErrorCode.UNAVAILABLE, cause=exc
                    ) from None
                await self._sleep(self._retry_delay(attempt))
            except httpx.RequestError as exc:
                raise MinerUFailure(
                    MinerUErrorCode.AMBIGUOUS_SUBMISSION, cause=exc
                ) from None

        if response is None:
            raise MinerUFailure(MinerUErrorCode.UNAVAILABLE) from None

        if response.status_code != 202:
            raise MinerUFailure(MinerUErrorCode.UPSTREAM_FAILURE) from None
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError
            upstream_task_id = self._required_string(payload, "task_id")
            status_url = self._validated_endpoint(
                self._required_string(payload, "status_url"),
                upstream_task_id,
                result=False,
            )
            result_url = self._validated_endpoint(
                self._required_string(payload, "result_url"),
                upstream_task_id,
                result=True,
            )
            queued_ahead = self._optional_count(payload, "queued_ahead")
            file_names = payload.get("file_names")
            if file_names is not None and (
                not isinstance(file_names, list)
                or any(not isinstance(item, str) for item in file_names)
            ):
                raise TypeError
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise MinerUFailure(
                MinerUErrorCode.AMBIGUOUS_SUBMISSION, cause=exc
            ) from None
        return MinerUSubmission(
            file_task_id=request.file_task_id,
            upstream_task_id=upstream_task_id,
            status_url=status_url,
            result_url=result_url,
            queued_ahead=queued_ahead,
        )

    async def _poll(
        self,
        submission: MinerUSubmission,
        deadline: float,
        progress_callback: ProgressCallback | None,
    ) -> None:
        retry_attempt = 0
        while True:
            if self._clock() >= deadline:
                raise MinerUFailure(MinerUErrorCode.DEADLINE_EXCEEDED) from None
            try:
                response = await self._client.get(
                    submission.status_url, timeout=self._timeout_before(deadline)
                )
            except asyncio.CancelledError:
                raise
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadError,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
            ) as exc:
                if retry_attempt >= self._settings.retry_attempts:
                    raise MinerUFailure(
                        MinerUErrorCode.UNAVAILABLE,
                        retry_file_task_safe=False,
                        cause=exc,
                    ) from None
                await self._retry_sleep_before_deadline(retry_attempt, deadline)
                retry_attempt += 1
                continue
            except httpx.RequestError as exc:
                raise MinerUFailure(
                    MinerUErrorCode.UNAVAILABLE,
                    retry_file_task_safe=False,
                    cause=exc,
                ) from None
            if self._is_transient_status(response.status_code):
                if retry_attempt >= self._settings.retry_attempts:
                    raise MinerUFailure(
                        MinerUErrorCode.UNAVAILABLE,
                        retry_file_task_safe=False,
                    ) from None
                await self._retry_sleep_before_deadline(retry_attempt, deadline)
                retry_attempt += 1
                continue
            if response.status_code != 200:
                raise MinerUFailure(MinerUErrorCode.UPSTREAM_FAILURE) from None
            retry_attempt = 0
            try:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TypeError
                status = self._required_string(payload, "status")
                queued_ahead = self._optional_count(payload, "queued_ahead")
            except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                raise MinerUFailure(MinerUErrorCode.INVALID_RESPONSE, cause=exc) from None
            if status == "completed":
                return
            if status == "failed":
                raise MinerUFailure(MinerUErrorCode.UPSTREAM_FAILURE) from None
            try:
                progress_status = MinerUProgressStatus(status)
            except ValueError as exc:
                raise MinerUFailure(MinerUErrorCode.INVALID_RESPONSE, cause=exc) from None
            if progress_callback is not None:
                callback_result = progress_callback(
                    MinerUProgress(
                        file_task_id=submission.file_task_id,
                        upstream_task_id=submission.upstream_task_id,
                        status=progress_status,
                        queued_ahead=queued_ahead,
                    )
                )
                if inspect.isawaitable(callback_result):
                    await callback_result
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise MinerUFailure(MinerUErrorCode.DEADLINE_EXCEEDED) from None
            await self._sleep(min(self._settings.poll_interval_seconds, remaining))

    async def _retry_sleep_before_deadline(
        self, attempt: int, deadline: float
    ) -> None:
        delay = self._retry_delay(attempt)
        if self._clock() + delay >= deadline:
            raise MinerUFailure(MinerUErrorCode.DEADLINE_EXCEEDED) from None
        await self._sleep(delay)

    def _retry_delay(self, attempt: int) -> float:
        return min(
            self._settings.retry_backoff_seconds * (2**attempt),
            self._settings.retry_max_backoff_seconds,
        )

    @staticmethod
    def _is_transient_status(status_code: int) -> bool:
        return status_code in {408, 429} or status_code >= 500

    def _timeout_before(self, deadline: float) -> httpx.Timeout:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise MinerUFailure(MinerUErrorCode.DEADLINE_EXCEEDED) from None
        return httpx.Timeout(
            connect=min(self._settings.connect_timeout_seconds, remaining),
            read=min(self._settings.read_timeout_seconds, remaining),
            write=min(self._settings.write_timeout_seconds, remaining),
            pool=min(self._settings.pool_timeout_seconds, remaining),
        )

    async def _download_and_publish(
        self,
        request: MinerUParseRequest,
        submission: MinerUSubmission,
        deadline: float,
    ) -> MinerUDocumentResult:
        request.output_directory.mkdir(parents=True, exist_ok=True)
        work_root = Path(
            tempfile.mkdtemp(prefix=".mineru-staging-", dir=request.output_directory)
        )
        archive_path = work_root / "result.zip"
        extracted_root = work_root / "extracted"
        try:
            await self._download_archive(
                submission.result_url, archive_path, deadline
            )
            extracted = await self._extract_archive_in_worker(
                archive_path,
                extracted_root,
                expected_stem=Path(request.upload_name).stem,
            )
            document_name = extracted.document_name
            parse_directory = extracted.parse_directory

            source_root = extracted_root / document_name
            published_root = request.output_directory / document_name
            try:
                publish_directory_no_replace(source_root, published_root)
            except FileExistsError as exc:
                raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE, cause=exc) from None
            parse_root = published_root / parse_directory
            expected_stem = Path(request.upload_name).stem
            markdown = parse_root / f"{expected_stem}.md"
            middle = parse_root / f"{expected_stem}_middle.json"
            legacy = parse_root / f"{expected_stem}_content_list.json"
            return MinerUDocumentResult(
                file_task_id=request.file_task_id,
                upstream_task_id=submission.upstream_task_id,
                result_root=published_root,
                markdown_path=markdown if markdown.is_file() else None,
                middle_json_path=middle if middle.is_file() else None,
                content_list_v2_path=parse_root
                / f"{expected_stem}_content_list_v2.json",
                legacy_content_list_path=legacy if legacy.is_file() else None,
                images_directory=parse_root / "images",
            )
        except asyncio.CancelledError:
            raise
        except MinerUFailure as exc:
            raise exc from None
        except (httpx.RequestError, OSError) as exc:
            raise MinerUFailure(
                MinerUErrorCode.UNAVAILABLE,
                retry_file_task_safe=False,
                cause=exc,
            ) from None
        finally:
            shutil.rmtree(work_root, ignore_errors=True)

    async def _download_archive(
        self, result_url: str, archive_path: Path, deadline: float
    ) -> None:
        for attempt in range(self._settings.retry_attempts + 1):
            try:
                async with self._client.stream(
                    "GET", result_url, timeout=self._timeout_before(deadline)
                ) as response:
                    if self._is_transient_status(response.status_code):
                        if attempt >= self._settings.retry_attempts:
                            raise MinerUFailure(
                                MinerUErrorCode.UNAVAILABLE,
                                retry_file_task_safe=False,
                            ) from None
                        await self._retry_sleep_before_deadline(attempt, deadline)
                        continue
                    if response.status_code != 200:
                        raise MinerUFailure(MinerUErrorCode.UPSTREAM_FAILURE) from None
                    self._validate_zip_response_headers(response)
                    total = 0
                    with archive_path.open("wb") as destination:
                        async for chunk in response.aiter_bytes():
                            if self._clock() >= deadline:
                                raise MinerUFailure(
                                    MinerUErrorCode.DEADLINE_EXCEEDED
                                ) from None
                            total += len(chunk)
                            if total > self._settings.max_compressed_bytes:
                                raise MinerUFailure(
                                    MinerUErrorCode.UNSAFE_ARCHIVE
                                ) from None
                            destination.write(chunk)
                    return
            except asyncio.CancelledError:
                raise
            except MinerUFailure:
                raise
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadError,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
            ) as exc:
                archive_path.unlink(missing_ok=True)
                if attempt >= self._settings.retry_attempts:
                    raise MinerUFailure(
                        MinerUErrorCode.UNAVAILABLE,
                        retry_file_task_safe=False,
                        cause=exc,
                    ) from None
                await self._retry_sleep_before_deadline(attempt, deadline)
        raise MinerUFailure(
            MinerUErrorCode.UNAVAILABLE, retry_file_task_safe=False
        ) from None

    def _validate_zip_response_headers(self, response: httpx.Response) -> None:
        content_type = response.headers.get("Content-Type", "")
        if content_type.partition(";")[0].strip().lower() not in {
            "application/zip",
            "application/x-zip-compressed",
        }:
            raise MinerUFailure(MinerUErrorCode.INVALID_RESPONSE) from None
        content_length = response.headers.get("Content-Length")
        if content_length is None:
            return
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise MinerUFailure(MinerUErrorCode.INVALID_RESPONSE, cause=exc) from None
        if declared_size < 0:
            raise MinerUFailure(MinerUErrorCode.INVALID_RESPONSE) from None
        if declared_size > self._settings.max_compressed_bytes:
            raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None

    async def _extract_archive_in_worker(
        self, archive_path: Path, extracted_root: Path, *, expected_stem: str
    ) -> ExtractedArchive:
        cancel_event = threading.Event()
        worker = asyncio.create_task(
            asyncio.to_thread(
                extract_archive,
                archive_path,
                extracted_root,
                expected_stem=expected_stem,
                max_entries=self._settings.max_archive_entries,
                max_uncompressed_bytes=self._settings.max_uncompressed_bytes,
                cancel_event=cancel_event,
            )
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as cancellation:
            cancel_event.set()
            await self._wait_for_archive_worker(worker)
            raise cancellation
        except ArchiveWorkCancelled as exc:
            raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE, cause=exc) from None

    @staticmethod
    async def _wait_for_archive_worker(worker: asyncio.Task[ExtractedArchive]) -> None:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                return
        if worker.cancelled():
            return
        try:
            worker.result()
        except Exception:
            pass

    @staticmethod
    def _required_string(payload: dict[str, Any], key: str) -> str:
        value = payload[key]
        if not isinstance(value, str) or not value:
            raise TypeError
        return value

    @staticmethod
    def _optional_count(payload: dict[str, Any], key: str) -> int | None:
        value = payload.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypeError
        return value

    def _validated_endpoint(
        self, reported_value: str, task_id: str, *, result: bool
    ) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", task_id) is None:
            raise ValueError
        suffix = f"tasks/{task_id}" + ("/result" if result else "")
        expected = httpx.URL(self._with_api_path(suffix))
        reported = httpx.URL(reported_value)
        if (
            not reported.is_absolute_url
            or reported.username
            or reported.password
            or reported.fragment
            or reported.query
            or reported.scheme != expected.scheme
            or reported.host != expected.host
            or reported.port != expected.port
            or reported.path != expected.path
        ):
            raise ValueError
        return str(expected)
