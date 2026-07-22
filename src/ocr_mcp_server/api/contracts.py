"""Strict, transport-neutral public API contracts."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ..domain.models import BatchStatus, FileStatus, ProcessingStage


_IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{7,255}\Z")
_TERMINAL_BATCH_STATUSES = frozenset(
    {
        BatchStatus.COMPLETED,
        BatchStatus.COMPLETED_WITH_ERRORS,
        BatchStatus.FAILED,
        BatchStatus.CANCELLED,
    }
)


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_uuid(value: str) -> str:
    parsed = UUID(value)
    canonical = str(parsed)
    if value != canonical:
        raise ValueError("identifier must be a canonical UUID")
    return canonical


CanonicalId = Annotated[
    str, Field(min_length=36, max_length=36), AfterValidator(_canonical_uuid)
]


class DocumentSource(StrictContract):
    file_id: CanonicalId | None = None
    url: AnyHttpUrl | None = None

    @field_validator("file_id")
    @classmethod
    def validate_file_id(cls, value: str | None) -> str | None:
        return None if value is None else _canonical_uuid(value)

    @field_validator("url")
    @classmethod
    def require_safe_https_url(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is None:
            return None
        if (
            value.scheme != "https"
            or value.username is not None
            or value.password is not None
            or value.fragment is not None
        ):
            raise ValueError("source URL must be an HTTPS resource URL")
        return value

    @model_validator(mode="after")
    def require_exactly_one_source(self) -> DocumentSource:
        if (self.file_id is None) == (self.url is None):
            raise ValueError("exactly one document source is required")
        return self


class UploadReceipt(StrictContract):
    file_id: CanonicalId
    size_bytes: int = Field(ge=1)
    media_type: Literal["application/pdf", "image/png", "image/jpeg"]

    _validate_file_id = field_validator("file_id")(_canonical_uuid)


class ParseDocumentsRequest(StrictContract):
    sources: list[DocumentSource] = Field(min_length=1, max_length=20)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str | None) -> str | None:
        if value is not None and _IDEMPOTENCY_RE.fullmatch(value) is None:
            raise ValueError("invalid idempotency key")
        return value


class ParseSubmission(StrictContract):
    batch_id: CanonicalId
    status: BatchStatus

    _validate_batch_id = field_validator("batch_id")(_canonical_uuid)


class ArtifactReference(StrictContract):
    artifact_id: CanonicalId
    download_url: AnyHttpUrl
    expires_at: datetime

    _validate_artifact_id = field_validator("artifact_id")(_canonical_uuid)

    @field_validator("download_url")
    @classmethod
    def require_https_download(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.scheme != "https" or value.username or value.password or value.fragment:
            raise ValueError("artifact URL must use HTTPS")
        return value


class SafeError(StrictContract):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    message: str = Field(min_length=1, max_length=160)


class FileStatusResponse(StrictContract):
    file_id: CanonicalId
    status: FileStatus
    stage: ProcessingStage
    progress: int = Field(ge=0, le=100)
    error: SafeError | None = None
    recovery_token: str | None = Field(default=None, min_length=8, max_length=256)

    _validate_file_id = field_validator("file_id")(_canonical_uuid)

    @field_validator("recovery_token")
    @classmethod
    def validate_recovery_token(cls, value: str | None) -> str | None:
        if value is not None and _TOKEN_RE.fullmatch(value) is None:
            raise ValueError("invalid recovery token")
        return value


class BatchStatusResponse(StrictContract):
    batch_id: CanonicalId
    status: BatchStatus
    progress: int = Field(ge=0, le=100)
    total_files: int = Field(ge=1, le=20)
    completed_files: int = Field(ge=0, le=20)
    failed_files: int = Field(ge=0, le=20)
    files: list[FileStatusResponse] = Field(max_length=20)
    artifacts: list[ArtifactReference] = Field(default_factory=list, max_length=20)

    _validate_batch_id = field_validator("batch_id")(_canonical_uuid)

    @model_validator(mode="after")
    def validate_result_invariants(self) -> BatchStatusResponse:
        if self.completed_files + self.failed_files > self.total_files:
            raise ValueError("terminal counts exceed total files")
        terminal = self.status in _TERMINAL_BATCH_STATUSES
        if terminal != (self.progress == 100):
            raise ValueError("terminal status and progress are inconsistent")
        if not terminal and self.artifacts:
            raise ValueError("artifacts are available only for terminal tasks")
        return self


class TaskStatusRequest(StrictContract):
    batch_id: CanonicalId

    _validate_batch_id = field_validator("batch_id")(_canonical_uuid)


class OrientationReparseRequest(StrictContract):
    recovery_token: str = Field(min_length=8, max_length=256)
    pages: list[int] | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("recovery_token")
    @classmethod
    def validate_recovery_token(cls, value: str) -> str:
        if _TOKEN_RE.fullmatch(value) is None:
            raise ValueError("invalid recovery token")
        return value

    @field_validator("pages")
    @classmethod
    def validate_pages(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and (
            any(isinstance(page, bool) or page <= 0 for page in value)
            or len(set(value)) != len(value)
        ):
            raise ValueError("pages must be unique positive integers")
        return value


class OrientationReparseSubmission(ParseSubmission):
    pass
