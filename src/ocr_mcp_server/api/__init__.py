"""FastAPI route package for the service's HTTP boundary."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health/live")
def liveness() -> dict[str, str]:
    """Report process liveness without touching databases or OCR models."""

    return {"status": "ok"}


__all__ = ["router"]
