"""Application port shared by REST and MCP transports."""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Callable
from typing import Protocol, TypeAlias

from .contracts import (
    BatchStatusResponse,
    OrientationReparseRequest,
    OrientationReparseSubmission,
    ParseDocumentsRequest,
    ParseSubmission,
    UploadReceipt,
)


ProgressCallback: TypeAlias = Callable[[int, int], Awaitable[None]]


class DocumentGateway(Protocol):
    async def upload_document(
        self,
        content: AsyncIterable[bytes],
        *,
        display_name: str,
        media_type: str,
        content_length: int | None,
        idempotency_key: str | None,
    ) -> UploadReceipt: ...

    async def parse_documents(
        self,
        request: ParseDocumentsRequest,
        *,
        progress: ProgressCallback | None = None,
    ) -> ParseSubmission: ...

    async def get_task_status(self, batch_id: str) -> BatchStatusResponse: ...

    async def reparse_with_page_orientation(
        self,
        request: OrientationReparseRequest,
        *,
        progress: ProgressCallback | None = None,
    ) -> OrientationReparseSubmission: ...


class GatewayFailure(Exception):
    """Stable, content-free application boundary failure."""

    code = "internal_error"
    safe_message = "The request could not be completed."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class GatewayNotFound(GatewayFailure):
    code = "not_found"
    safe_message = "The requested resource was not found."


class GatewayConflict(GatewayFailure):
    code = "conflict"
    safe_message = "The request conflicts with the current resource state."


class GatewayCapacityExceeded(GatewayFailure):
    code = "capacity_exceeded"
    safe_message = "The request exceeds a service capacity limit."


class GatewayUploadTooLarge(GatewayCapacityExceeded):
    """Transport-specific status while retaining the stable capacity code."""


class GatewayUnavailable(GatewayFailure):
    code = "service_unavailable"
    safe_message = "The service is temporarily unavailable."


class GatewayInvalidRequest(GatewayFailure):
    code = "invalid_request"
    safe_message = "The request is invalid."


class GatewayUnsupportedMediaType(GatewayFailure):
    code = "unsupported_media_type"
    safe_message = "The document media type is not supported."


class GatewayOrientationUncertain(GatewayFailure):
    code = "orientation_uncertain"
    safe_message = "A reliable non-zero page orientation could not be determined."
