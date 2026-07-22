"""Composable gateway bridge for the opt-in orientation recovery path."""

from __future__ import annotations

from .gateway import (
    DocumentGateway,
    GatewayConflict,
    GatewayFailure,
    GatewayInvalidRequest,
    GatewayNotFound,
    GatewayOrientationUncertain,
    GatewayUnavailable,
    ProgressCallback,
)
from .contracts import OrientationReparseRequest, OrientationReparseSubmission
from ..services.orientation_recovery import (
    OrientationRecoveryCommand,
    RecoveryServiceErrorCode,
    RecoveryServiceFailure,
)


class OrientationRecoveryGateway:
    """Delegate normal gateway methods and own only confirmed recovery."""

    def __init__(self, delegate: DocumentGateway, recovery) -> None:
        self._delegate = delegate
        self._recovery = recovery

    async def upload_document(self, content, **kwargs):
        return await self._delegate.upload_document(content, **kwargs)

    async def parse_documents(self, request, *, progress: ProgressCallback | None = None):
        return await self._delegate.parse_documents(request, progress=progress)

    async def get_task_status(self, batch_id: str):
        return await self._delegate.get_task_status(batch_id)

    async def reparse_with_page_orientation(
        self,
        request: OrientationReparseRequest,
        *,
        progress: ProgressCallback | None = None,
    ):
        try:
            result = await self._recovery.reparse(
                OrientationRecoveryCommand(
                    recovery_token=request.recovery_token,
                    pages=None if request.pages is None else tuple(request.pages),
                ),
                progress=progress,
            )
            return OrientationReparseSubmission(
                batch_id=result.batch_id, status=result.status
            )
        except RecoveryServiceFailure as exc:
            raise _gateway_failure(exc.code) from None
        except ValueError:
            raise GatewayInvalidRequest() from None
        except GatewayFailure:
            raise
        except Exception:
            raise GatewayUnavailable() from None


def _gateway_failure(code: RecoveryServiceErrorCode) -> GatewayFailure:
    return {
        RecoveryServiceErrorCode.TOKEN_INVALID: GatewayNotFound,
        RecoveryServiceErrorCode.REQUEST_INVALID: GatewayInvalidRequest,
        RecoveryServiceErrorCode.CONFLICT: GatewayConflict,
        RecoveryServiceErrorCode.UNCERTAIN: GatewayOrientationUncertain,
        RecoveryServiceErrorCode.UNAVAILABLE: GatewayUnavailable,
        RecoveryServiceErrorCode.PROCESSING_FAILED: GatewayFailure,
    }[code]()
