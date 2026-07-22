"""Thin FastAPI transport adapter over :class:`DocumentGateway`."""

from __future__ import annotations

from collections.abc import AsyncIterator
import re
from typing import Awaitable, TypeVar

from fastapi import APIRouter, Request, status

from .contracts import (
    BatchStatusResponse,
    CanonicalId,
    OrientationReparseRequest,
    OrientationReparseSubmission,
    ParseDocumentsRequest,
    ParseSubmission,
    UploadReceipt,
)
from .gateway import (
    DocumentGateway,
    GatewayFailure,
    GatewayCapacityExceeded,
    GatewayInvalidRequest,
    GatewayUploadTooLarge,
    GatewayUnavailable,
    GatewayUnsupportedMediaType,
)


router = APIRouter(prefix="/v1")
_MEDIA_TYPES = frozenset({"application/pdf", "image/png", "image/jpeg"})
_DISPLAY_NAME_RE = re.compile(r"[^/\\\x00-\x1f\x7f]{1,255}\Z")
_IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_ResultT = TypeVar("_ResultT")


def _gateway(request: Request) -> DocumentGateway:
    gateway = request.app.state.gateway
    if gateway is None:
        raise GatewayUnavailable()
    return gateway


async def _safe_gateway_call(awaitable: Awaitable[_ResultT]) -> _ResultT:
    try:
        return await awaitable
    except GatewayFailure:
        raise
    except Exception:
        raise GatewayFailure() from None


@router.post(
    "/uploads",
    response_model=UploadReceipt,
    status_code=status.HTTP_201_CREATED,
    operation_id="uploadDocument",
)
async def upload_document(request: Request) -> UploadReceipt:
    display_name = request.headers.get("x-document-name")
    if display_name is None or _DISPLAY_NAME_RE.fullmatch(display_name) is None:
        raise GatewayInvalidRequest()
    idempotency_key = request.headers.get("idempotency-key")
    if (
        idempotency_key is not None
        and _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None
    ):
        raise GatewayInvalidRequest()
    raw_media_type = request.headers.get("content-type", "")
    if len(raw_media_type) > 128:
        raise GatewayInvalidRequest()
    media_type = raw_media_type.partition(";")[0].strip().lower()
    if media_type not in _MEDIA_TYPES:
        raise GatewayUnsupportedMediaType()
    raw_length = request.headers.get("content-length")
    try:
        content_length = None if raw_length is None else int(raw_length)
    except ValueError:
        raise GatewayInvalidRequest() from None
    limit = request.app.state.settings.limits.max_file_size_bytes
    if content_length is not None and content_length <= 0:
        raise GatewayInvalidRequest()
    if content_length is not None and content_length > limit:
        raise GatewayUploadTooLarge()

    source = request.stream().__aiter__()
    first = b""
    async for candidate in source:
        if candidate:
            first = candidate
            break
    if not first:
        raise GatewayInvalidRequest()

    async def bounded_content() -> AsyncIterator[bytes]:
        total = len(first)
        if total > limit:
            raise GatewayUploadTooLarge()
        yield first
        async for chunk in source:
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                raise GatewayUploadTooLarge()
            yield chunk

    return await _safe_gateway_call(
        _gateway(request).upload_document(
            bounded_content(),
            display_name=display_name,
            media_type=media_type,
            content_length=content_length,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/tasks",
    response_model=ParseSubmission,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="parseDocuments",
)
async def parse_documents(
    payload: ParseDocumentsRequest, request: Request
) -> ParseSubmission:
    return await _safe_gateway_call(_gateway(request).parse_documents(payload))


@router.get(
    "/tasks/{batch_id}",
    response_model=BatchStatusResponse,
    operation_id="getTaskStatus",
)
async def get_task_status(batch_id: CanonicalId, request: Request) -> BatchStatusResponse:
    return await _safe_gateway_call(_gateway(request).get_task_status(batch_id))


@router.post(
    "/orientation-reparse",
    response_model=OrientationReparseSubmission,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="reparseWithPageOrientation",
)
async def reparse_with_page_orientation(
    payload: OrientationReparseRequest, request: Request
) -> OrientationReparseSubmission:
    return await _safe_gateway_call(
        _gateway(request).reparse_with_page_orientation(payload)
    )
