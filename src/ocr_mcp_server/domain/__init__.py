"""Shared domain contracts for the OCR service."""

from .errors import (
    ConfigurationError,
    DomainError,
    InputValidationError,
    LeaseConflictError,
    PersistenceError,
    StateTransitionError,
)
from .models import (
    BatchStatus,
    FileStatus,
    ProcessingStage,
    SecondaryOCREngine,
    new_id,
    utc_now,
)
from .tasks import BatchSnapshot, CreateBatchResult, FileTaskSnapshot, LeaseClaim

__all__ = [
    "BatchStatus",
    "BatchSnapshot",
    "ConfigurationError",
    "DomainError",
    "FileStatus",
    "InputValidationError",
    "LeaseClaim",
    "LeaseConflictError",
    "PersistenceError",
    "ProcessingStage",
    "SecondaryOCREngine",
    "StateTransitionError",
    "CreateBatchResult",
    "FileTaskSnapshot",
    "new_id",
    "utc_now",
]
