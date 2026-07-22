"""Three curated FastMCP tools over the shared document gateway port."""

import math
from typing import Annotated

from fastmcp import Context, FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from pydantic import BaseModel, Field, StrictInt, ValidationError

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


McpSources = Annotated[
    list[DocumentSource], Field(min_length=1, max_length=20)
]
McpIdempotencyKey = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
]
McpRecoveryToken = Annotated[str, Field(min_length=8, max_length=256)]
McpPages = Annotated[
    list[Annotated[StrictInt, Field(gt=0)]], Field(min_length=1, max_length=500)
]


class _SafeFastMCP(FastMCP):
    """Contain framework errors raised outside the FastMCP middleware chain."""

    async def _call_tool_mcp(self, key, arguments):
        try:
            return await super()._call_tool_mcp(key, arguments)
        except ToolError:
            raise
        except NotFoundError:
            raise ToolError(
                "not_found: The requested tool was not found."
            ) from None
        except Exception:
            raise ToolError(
                "internal_error: The request could not be completed."
            ) from None


def create_mcp_server(gateway: DocumentGateway | None) -> FastMCP:
    """Create only the approved weak-agent-facing tools."""

    mcp = _SafeFastMCP(
        "OCR Document Gateway",
        middleware=[_SafeValidationMiddleware()],
        mask_error_details=True,
        # SDK-level JSON Schema errors echo input values and run outside middleware.
        # FunctionTool still validates against the same published schema inside the
        # safe middleware chain.
        strict_input_validation=False,
    )

    @mcp.tool(
        name="parse_documents",
        description="Submit 1 to 20 uploaded files or approved HTTPS URLs for OCR.",
    )
    async def parse_documents(
        sources: McpSources,
        idempotency_key: McpIdempotencyKey | None = None,
        ctx: Context | None = None,
    ) -> ParseSubmission:
        request = ParseDocumentsRequest(
            sources=sources, idempotency_key=idempotency_key
        )
        result = await _safe_call(
            _require_gateway(gateway).parse_documents(
                request, progress=_ProgressBridge(ctx) if ctx is not None else None
            )
        )
        return _validated_output(ParseSubmission, result)

    @mcp.tool(
        name="get_task_status",
        description="Get current progress and final artifact links for an OCR task.",
    )
    async def get_task_status(batch_id: CanonicalId) -> BatchStatusResponse:
        result = await _safe_call(
            _require_gateway(gateway).get_task_status(batch_id)
        )
        return _validated_output(BatchStatusResponse, result)

    @mcp.tool(
        name="reparse_with_page_orientation",
        description="Reparse approved pages using an existing recovery token.",
    )
    async def reparse_with_page_orientation(
        recovery_token: McpRecoveryToken,
        pages: McpPages | None = None,
        ctx: Context | None = None,
    ) -> OrientationReparseSubmission:
        request = OrientationReparseRequest(
            recovery_token=recovery_token, pages=pages
        )
        result = await _safe_call(
            _require_gateway(gateway).reparse_with_page_orientation(
                request, progress=_ProgressBridge(ctx) if ctx is not None else None
            )
        )
        return _validated_output(OrientationReparseSubmission, result)

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


def _validated_output(model: type[BaseModel], value):
    try:
        return model.model_validate(value)
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
