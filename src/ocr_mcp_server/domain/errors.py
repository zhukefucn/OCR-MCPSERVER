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


class MinerUErrorCode(StrEnum):
    """Stable machine codes for failures at the MinerU boundary."""

    UNAVAILABLE = "mineru_unavailable"
    AMBIGUOUS_SUBMISSION = "mineru_submission_ambiguous"
    DEADLINE_EXCEEDED = "mineru_deadline_exceeded"
    UPSTREAM_FAILURE = "mineru_upstream_failed"
    INVALID_RESPONSE = "mineru_response_invalid"
    UNSAFE_ARCHIVE = "mineru_archive_unsafe"


_MINERU_SAFE_MESSAGES = {
    MinerUErrorCode.UNAVAILABLE: "The document parsing service is unavailable.",
    MinerUErrorCode.AMBIGUOUS_SUBMISSION: "The document submission outcome is uncertain.",
    MinerUErrorCode.DEADLINE_EXCEEDED: "The document parsing deadline was exceeded.",
    MinerUErrorCode.UPSTREAM_FAILURE: "The document parsing service rejected or failed the task.",
    MinerUErrorCode.INVALID_RESPONSE: "The document parsing service returned an invalid response.",
    MinerUErrorCode.UNSAFE_ARCHIVE: "The document parsing result archive is unsafe or too large.",
}


class MinerUFailure(DomainError):
    """A safe MinerU error that never retains an unsafe upstream cause."""

    def __init__(
        self,
        code: MinerUErrorCode,
        *,
        retry_file_task_safe: bool | None = None,
        cause: BaseException | None = None,
    ) -> None:
        del cause
        self.code = code.value
        self.safe_message = _MINERU_SAFE_MESSAGES[code]
        self.retry_file_task_safe = (
            code is MinerUErrorCode.UNAVAILABLE
            if retry_file_task_safe is None
            else retry_file_task_safe
        )
        Exception.__init__(self, self.safe_message)


class CandidateCollectionErrorCode(StrEnum):
    """Stable machine codes for image-candidate collection failures."""

    INVALID_MANIFEST = "candidate_manifest_invalid"
    UNSAFE_OR_MISSING_PATH = "candidate_path_unsafe_or_missing"
    INVALID_IMAGE = "candidate_image_invalid_or_unsupported"
    CHANGED_DURING_INSPECTION = "candidate_changed_during_inspection"
    INVARIANT_VIOLATION = "candidate_collection_invariant"


_CANDIDATE_COLLECTION_SAFE_MESSAGES = {
    CandidateCollectionErrorCode.INVALID_MANIFEST: "The structured OCR result is invalid.",
    CandidateCollectionErrorCode.UNSAFE_OR_MISSING_PATH: "A candidate image path is unsafe or unavailable.",
    CandidateCollectionErrorCode.INVALID_IMAGE: "A candidate image is invalid or unsupported.",
    CandidateCollectionErrorCode.CHANGED_DURING_INSPECTION: "A candidate image changed during inspection.",
    CandidateCollectionErrorCode.INVARIANT_VIOLATION: "The candidate collection is inconsistent.",
}


class CandidateCollectionFailure(DomainError):
    """A candidate-collection failure that never retains unsafe cause text."""

    def __init__(
        self,
        code: CandidateCollectionErrorCode,
        *,
        cause: BaseException | None = None,
    ) -> None:
        del cause
        self.code = code.value
        self.safe_message = _CANDIDATE_COLLECTION_SAFE_MESSAGES[code]
        Exception.__init__(self, self.safe_message)


class SecondaryOcrErrorCode(StrEnum):
    """Stable machine codes for the secondary OCR execution boundary."""

    INITIALIZATION_UNAVAILABLE = "secondary_ocr_initialization_unavailable"
    QUEUE_SATURATED = "secondary_ocr_queue_saturated"
    NOT_STARTED = "secondary_ocr_not_started"
    INTERNAL_WORKER_FAILURE = "secondary_ocr_internal_worker_failure"


_SECONDARY_OCR_SAFE_MESSAGES = {
    SecondaryOcrErrorCode.INITIALIZATION_UNAVAILABLE: "The secondary OCR provider is unavailable.",
    SecondaryOcrErrorCode.QUEUE_SATURATED: "The secondary OCR queue is at capacity.",
    SecondaryOcrErrorCode.NOT_STARTED: "The secondary OCR provider is not accepting work.",
    SecondaryOcrErrorCode.INTERNAL_WORKER_FAILURE: "The secondary OCR worker failed safely.",
}


class SecondaryOcrFailure(DomainError):
    """A safe secondary-OCR failure that discards all backend details."""

    def __init__(
        self,
        code: SecondaryOcrErrorCode,
        *,
        cause: BaseException | None = None,
    ) -> None:
        del cause
        self.code = code.value
        self.safe_message = _SECONDARY_OCR_SAFE_MESSAGES[code]
        Exception.__init__(self, self.safe_message)


class MergeErrorCode(StrEnum):
    """Stable safe codes for merge, publication, and rollback failures."""

    INVALID_COVERAGE = "merge_coverage_invalid"
    INVARIANT_VIOLATION = "merge_invariant_invalid"
    INVALID_SOURCE_MANIFEST = "merge_source_manifest_invalid"
    INVALID_STRUCTURED_CONTENT = "merge_structured_content_invalid"
    UNSAFE_REFERENCE = "merge_reference_unsafe"
    UNSAFE_PUBLICATION_PATH = "merge_publication_path_unsafe"
    PUBLICATION_CONFLICT = "merge_publication_conflict"
    PUBLICATION_FAILED = "merge_publication_failed"
    ROLLBACK_VERIFICATION_FAILED = "merge_rollback_verification_failed"


_MERGE_SAFE_MESSAGES = {
    MergeErrorCode.INVALID_COVERAGE: "Secondary OCR result coverage is invalid.",
    MergeErrorCode.INVARIANT_VIOLATION: "Merge inputs are inconsistent.",
    MergeErrorCode.INVALID_SOURCE_MANIFEST: "The source structured result is invalid.",
    MergeErrorCode.INVALID_STRUCTURED_CONTENT: "Structured recognized content is invalid.",
    MergeErrorCode.UNSAFE_REFERENCE: "A structured result reference is unsafe.",
    MergeErrorCode.UNSAFE_PUBLICATION_PATH: "The publication location is unsafe.",
    MergeErrorCode.PUBLICATION_CONFLICT: "The result version already contains different content.",
    MergeErrorCode.PUBLICATION_FAILED: "The result version could not be published.",
    MergeErrorCode.ROLLBACK_VERIFICATION_FAILED: "The rollback source could not be verified.",
}


class MergeFailure(DomainError):
    """A merge-layer failure that never retains unsafe content or causes."""

    def __init__(self, code: MergeErrorCode, *, cause: BaseException | None = None) -> None:
        del cause
        self.code = code.value
        self.safe_message = _MERGE_SAFE_MESSAGES[code]
        Exception.__init__(self, self.safe_message)


class ArtifactErrorCode(StrEnum):
    """Stable content-free codes for result artifact generation and indexing."""

    INVALID_INPUT = "artifact_input_invalid"
    UNSAFE_SOURCE = "artifact_source_unsafe"
    UNSAFE_IMAGE = "artifact_image_unsafe"
    UNSUPPORTED_NODE = "artifact_markdown_node_unsupported"
    LIMIT_EXCEEDED = "artifact_limit_exceeded"
    PUBLISH_CONFLICT = "artifact_publish_conflict"
    PUBLISH_FAILED = "artifact_publish_failed"
    INDEX_CONFLICT = "artifact_index_conflict"
    INDEX_FAILED = "artifact_index_failed"


_ARTIFACT_SAFE_MESSAGES = {
    ArtifactErrorCode.INVALID_INPUT: "Artifact inputs are invalid or inconsistent.",
    ArtifactErrorCode.UNSAFE_SOURCE: "An artifact source is unsafe or unavailable.",
    ArtifactErrorCode.UNSAFE_IMAGE: "A referenced artifact image is unsafe or unavailable.",
    ArtifactErrorCode.UNSUPPORTED_NODE: "A structured node cannot be rendered.",
    ArtifactErrorCode.LIMIT_EXCEEDED: "An artifact limit was exceeded.",
    ArtifactErrorCode.PUBLISH_CONFLICT: "The artifact location contains different content.",
    ArtifactErrorCode.PUBLISH_FAILED: "The artifact could not be published.",
    ArtifactErrorCode.INDEX_CONFLICT: "Artifact metadata conflicts with an immutable record.",
    ArtifactErrorCode.INDEX_FAILED: "Artifact metadata could not be registered.",
}


class ArtifactFailure(DomainError):
    """Artifact failure that discards paths, content, backend text, and causes."""

    def __init__(
        self, code: ArtifactErrorCode, *, cause: BaseException | None = None
    ) -> None:
        del cause
        self.code = code.value
        self.safe_message = _ARTIFACT_SAFE_MESSAGES[code]
        Exception.__init__(self, self.safe_message)
