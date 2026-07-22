"""Shared domain contracts for the OCR service."""

from .errors import (
    ConfigurationError,
    DomainError,
    FileIntakeErrorCode,
    FileIntakeFailure,
    InputValidationError,
    LeaseConflictError,
    MinerUErrorCode,
    MinerUFailure,
    PersistenceError,
    StateTransitionError,
)
from .files import IncomingFile, StoredFile, SupportedMediaType
from .models import (
    BatchStatus,
    FileStatus,
    ProcessingStage,
    SecondaryOCREngine,
    new_id,
    utc_now,
)
from .mineru import (
    MinerUDocumentResult,
    MinerUParseRequest,
    MinerUProgress,
    MinerUProgressStatus,
    MinerUSubmission,
)
from .tasks import BatchSnapshot, CreateBatchResult, FileTaskSnapshot, LeaseClaim

__all__ = [
    "BatchStatus",
    "BatchSnapshot",
    "ConfigurationError",
    "DomainError",
    "FileIntakeErrorCode",
    "FileIntakeFailure",
    "FileStatus",
    "InputValidationError",
    "IncomingFile",
    "LeaseClaim",
    "LeaseConflictError",
    "MinerUDocumentResult",
    "MinerUErrorCode",
    "MinerUFailure",
    "MinerUParseRequest",
    "MinerUProgress",
    "MinerUProgressStatus",
    "MinerUSubmission",
    "PersistenceError",
    "ProcessingStage",
    "SecondaryOCREngine",
    "StateTransitionError",
    "StoredFile",
    "SupportedMediaType",
    "CreateBatchResult",
    "FileTaskSnapshot",
    "new_id",
    "utc_now",
]
