"""Shared domain contracts for the OCR service."""

from .errors import ConfigurationError, DomainError, InputValidationError
from .models import (
    BatchStatus,
    FileStatus,
    ProcessingStage,
    SecondaryOCREngine,
    new_id,
    utc_now,
)

__all__ = [
    "BatchStatus",
    "ConfigurationError",
    "DomainError",
    "FileStatus",
    "InputValidationError",
    "ProcessingStage",
    "SecondaryOCREngine",
    "new_id",
    "utc_now",
]
