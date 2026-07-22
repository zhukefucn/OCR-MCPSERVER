"""Thin FastAPI transport adapter over :class:`DocumentGateway`."""

from __future__ import annotations

from collections.abc import AsyncIterator
import re
from typing import Awaitable, TypeVar

from fastapi import APIRouter, Request, status
from pydantic import BaseModel

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


async def _safe_gateway_call(
    awaitable: Awaitable[object], result_type: type[_ResultT]
) -> _ResultT:
    try:
        result = await awaitable
        if not issubclass(result_type, BaseModel):
            raise TypeError("gateway result type must be a Pydantic model")
        return result_type.model_validate(result)
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
    display_names = _raw_header_values(request, b"x-document-name")
    if len(display_names) != 1:
        raise GatewayInvalidRequest()
    display_name = display_names[0]
    if _DISPLAY_NAME_RE.fullmatch(display_name) is None:
        raise GatewayInvalidRequest()
    idempotency_keys = _raw_header_values(request, b"idempotency-key")
    if len(idempotency_keys) > 1:
        raise GatewayInvalidRequest()
    idempotency_key = idempotency_keys[0] if idempotency_keys else None
    if (
        idempotency_key is not None
        and _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None
    ):
        raise GatewayInvalidRequest()
    media_types = _raw_header_values(request, b"content-type")
    if len(media_types) != 1:
        raise GatewayInvalidRequest()
    raw_media_type = media_types[0]
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
        ),
        UploadReceipt,
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
    return await _safe_gateway_call(
        _gateway(request).parse_documents(payload), ParseSubmission
    )


@router.get(
    "/tasks/{batch_id}",
    response_model=BatchStatusResponse,
    operation_id="getTaskStatus",
)
async def get_task_status(batch_id: CanonicalId, request: Request) -> BatchStatusResponse:
    return await _safe_gateway_call(
        _gateway(request).get_task_status(batch_id), BatchStatusResponse
    )


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
        _gateway(request).reparse_with_page_orientation(payload),
        OrientationReparseSubmission,
    )


def _raw_header_values(request: Request, name: bytes) -> list[str]:
    values: list[str] = []
    for raw_name, raw_value in request.scope.get("headers", ()):
        if raw_name.lower() == name:
            values.append(raw_value.decode("latin-1"))
    return values
