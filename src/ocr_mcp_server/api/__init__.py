"""FastAPI route package for the service's HTTP boundary."""

from fastapi import APIRouter

from .rest import router as rest_router

router = APIRouter()


@router.get("/health/live", operation_id="liveness")
def liveness() -> dict[str, str]:
    """Report process liveness without touching databases or OCR models."""

    return {"status": "ok"}


router.include_router(rest_router)


__all__ = ["router"]
