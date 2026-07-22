"""Safe, stable errors shared across service layers."""

from __future__ import annotations

from enum import StrEnum


class DomainError(Exception):
    """Base error whose string representation is safe for clients and logs."""

    code = "domain_error"
    safe_message = "The operation could not be completed."

    def __init__(self, *, cause: BaseException | None = None) -> None:
        del cause
        super().__init__(self.safe_message)


class ConfigurationError(DomainError):
    """Raised when deployment configuration cannot be loaded or validated."""

    code = "configuration_invalid"
    safe_message = "Service configuration is invalid."


class InputValidationError(DomainError):
    """Raised when an incoming business input violates the service contract."""

    code = "input_invalid"
    safe_message = "Input validation failed."


class StateTransitionError(DomainError):
    """Raised when a requested task state change violates domain rules."""

    code = "state_transition_invalid"
    safe_message = "Task state transition is invalid."


class LeaseConflictError(DomainError):
    """Raised when a task lease is absent, expired, or does not match."""

    code = "lease_conflict"
    safe_message = "Task lease is invalid or expired."


class PersistenceError(DomainError):
    """Raised when task persistence fails without exposing database details."""

    code = "persistence_error"
    safe_message = "Task persistence operation failed."


class FileIntakeErrorCode(StrEnum):
    """Stable machine codes for file intake failures."""

    UNSUPPORTED_TYPE = "file_type_unsupported"
    MIME_MAGIC_MISMATCH = "file_type_mismatch"
    TOO_LARGE = "file_too_large"
    TOO_MANY_PAGES = "pdf_too_many_pages"
    ENCRYPTED_PDF = "pdf_encrypted"
    INVALID_DOCUMENT = "document_invalid"
    TOO_MANY_PIXELS = "image_too_many_pixels"
    UNSAFE_PATH = "path_unsafe"
    BATCH_CAPACITY_EXCEEDED = "batch_capacity_exceeded"
    REMOTE_URL_REJECTED = "remote_url_rejected"
    REMOTE_FETCH_FAILED = "remote_fetch_failed"


_FILE_INTAKE_SAFE_MESSAGES = {
    FileIntakeErrorCode.UNSUPPORTED_TYPE: "The file type is not supported.",
    FileIntakeErrorCode.MIME_MAGIC_MISMATCH: "The file type metadata does not match its content.",
    FileIntakeErrorCode.TOO_LARGE: "The file exceeds the allowed size.",
    FileIntakeErrorCode.TOO_MANY_PAGES: "The PDF exceeds the allowed page count.",
    FileIntakeErrorCode.ENCRYPTED_PDF: "Encrypted PDFs are not supported.",
    FileIntakeErrorCode.INVALID_DOCUMENT: "The document is empty, corrupt, or invalid.",
    FileIntakeErrorCode.TOO_MANY_PIXELS: "The image exceeds the allowed pixel count.",
    FileIntakeErrorCode.UNSAFE_PATH: "The generated storage path is unsafe.",
    FileIntakeErrorCode.BATCH_CAPACITY_EXCEEDED: "The batch exceeds its capacity limit.",
    FileIntakeErrorCode.REMOTE_URL_REJECTED: "The remote URL is not allowed.",
    FileIntakeErrorCode.REMOTE_FETCH_FAILED: "The remote file could not be fetched.",
}


class FileIntakeFailure(DomainError):
    """A file-intake error that deliberately discards unsafe cause text."""

    def __init__(
        self,
        code: FileIntakeErrorCode,
        *,
        cause: BaseException | None = None,
    ) -> None:
        del cause
        self.code = code.value
        self.safe_message = _FILE_INTAKE_SAFE_MESSAGES[code]
        Exception.__init__(self, self.safe_message)
