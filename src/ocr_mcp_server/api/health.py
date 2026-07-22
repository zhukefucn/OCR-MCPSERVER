"""Liveness and strict readiness HTTP routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..services.health import DependencyStatus


router = APIRouter()


@router.get("/health/live", operation_id="liveness")
def liveness() -> dict[str, str]:
    """Report process liveness without touching dependencies."""

    return {"status": "ok"}


@router.get("/health/ready", operation_id="readiness")
async def readiness(request: Request) -> JSONResponse:
    snapshot = await request.app.state.readiness.check()
    body = {
        "status": snapshot.status.value,
        "dependencies": [
            {
                "dependency": result.dependency.value,
                "status": result.status.value,
                "code": result.code.value,
            }
            for result in snapshot.dependencies
        ],
    }
    return JSONResponse(
        status_code=200 if snapshot.status is DependencyStatus.READY else 503,
        content=body,
    )


__all__ = ["router"]
