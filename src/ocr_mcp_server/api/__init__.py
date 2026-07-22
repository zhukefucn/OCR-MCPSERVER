"""FastAPI route package for the service's HTTP boundary."""

from fastapi import APIRouter

from .health import router as health_router
from .rest import router as rest_router

router = APIRouter()
router.include_router(health_router)
router.include_router(rest_router)


__all__ = ["router"]
