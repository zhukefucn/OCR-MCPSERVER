"""Three curated FastMCP tools over the shared document gateway port."""

import math

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from pydantic import ValidationError

from .contracts import (
    BatchStatusResponse,
    CanonicalId,
    DocumentSource,
    OrientationReparseRequest,
    OrientationReparseSubmission,
    ParseDocumentsRequest,
    ParseSubmission,
)
from .gateway import DocumentGateway, GatewayFailure, GatewayUnavailable


def create_mcp_server(gateway: DocumentGateway | None) -> FastMCP:
    """Create only the approved weak-agent-facing tools."""

    mcp = FastMCP(
        "OCR Document Gateway",
        middleware=[_SafeValidationMiddleware()],
        mask_error_details=True,
        strict_input_validation=True,
    )

    @mcp.tool(
        name="parse_documents",
        description="Submit 1 to 20 uploaded files or approved HTTPS URLs for OCR.",
    )
    async def parse_documents(
        sources: list[DocumentSource],
        idempotency_key: str | None = None,
        ctx: Context | None = None,
    ) -> ParseSubmission:
        request = ParseDocumentsRequest(
            sources=sources, idempotency_key=idempotency_key
        )
        return await _safe_call(
            _require_gateway(gateway).parse_documents(
                request, progress=_ProgressBridge(ctx) if ctx is not None else None
            )
        )

    @mcp.tool(
        name="get_task_status",
        description="Get current progress and final artifact links for an OCR task.",
    )
    async def get_task_status(batch_id: CanonicalId) -> BatchStatusResponse:
        return await _safe_call(_require_gateway(gateway).get_task_status(batch_id))

    @mcp.tool(
        name="reparse_with_page_orientation",
        description="Reparse approved pages using an existing recovery token.",
    )
    async def reparse_with_page_orientation(
        recovery_token: str,
        pages: list[int] | None = None,
        ctx: Context | None = None,
    ) -> OrientationReparseSubmission:
        request = OrientationReparseRequest(
            recovery_token=recovery_token, pages=pages
        )
        return await _safe_call(
            _require_gateway(gateway).reparse_with_page_orientation(
                request, progress=_ProgressBridge(ctx) if ctx is not None else None
            )
        )

    return mcp


def _require_gateway(gateway: DocumentGateway | None) -> DocumentGateway:
    if gateway is None:
        raise ToolError(
            f"{GatewayUnavailable.code}: {GatewayUnavailable.safe_message}"
        )
    return gateway


async def _safe_call(awaitable):
    try:
        return await awaitable
    except ToolError:
        raise
    except GatewayFailure as exc:
        raise ToolError(f"{exc.code}: {exc.safe_message}") from None
    except Exception:
        raise ToolError(
            "internal_error: The request could not be completed."
        ) from None


class _ProgressBridge:
    """Forward increasing numeric progress without content-bearing messages."""

    def __init__(self, context: Context) -> None:
        self._context = context
        self._last = -math.inf

    async def __call__(self, progress: int, total: int) -> None:
        if (
            isinstance(progress, bool)
            or isinstance(total, bool)
            or not isinstance(progress, (int, float))
            or not isinstance(total, (int, float))
            or not math.isfinite(progress)
            or not math.isfinite(total)
            or total <= 0
            or progress < 0
            or progress > total
            or progress <= self._last
        ):
            return
        self._last = progress
        await self._context.report_progress(progress=progress, total=total)


class _SafeValidationMiddleware(Middleware):
    """Replace value-bearing Pydantic diagnostics with a stable safe error."""

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ):
        try:
            return await call_next(context)
        except ValidationError:
            raise ToolError("invalid_request: The request is invalid.") from None
