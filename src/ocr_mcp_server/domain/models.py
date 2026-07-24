"""Fundamental domain enumerations and value generators."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


class BatchStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FileStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProcessingStage(StrEnum):
    UPLOADING = "uploading"
    VALIDATING = "validating"
    QUEUED = "queued"
    MINERU_PARSING = "mineru_parsing"
    COLLECTING_IMAGES = "collecting_images"
    DETECTING_ORIENTATION = "detecting_orientation"
    CLASSIFYING_IMAGES = "classifying_images"
    RECOGNIZING_IMAGES = "recognizing_images"
    MERGING = "merging"
    PACKAGING = "packaging"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SecondaryOCREngine(StrEnum):
    PP_STRUCTURE_V3 = "pp_structure_v3"
    PADDLEOCR_VL = "paddleocr_vl"


def utc_now() -> datetime:
    """Return the current timezone-aware UTC time."""

    return datetime.now(UTC)


def new_id() -> str:
    """Return a new UUID as its canonical string representation."""

    return str(uuid4())
