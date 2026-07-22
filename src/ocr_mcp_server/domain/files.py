"""Safe file metadata contracts used by intake services."""

from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class SupportedMediaType(StrEnum):
    """The only document media types accepted by this service."""

    PDF = "application/pdf"
    PNG = "image/png"
    JPEG = "image/jpeg"


@dataclass(frozen=True, slots=True)
class IncomingFile:
    """Caller-supplied metadata and a one-pass byte stream."""

    display_name: str
    declared_mime: str
    content: AsyncIterable[bytes]


@dataclass(frozen=True, slots=True)
class StoredFile:
    """Server-generated metadata for a safely persisted file."""

    file_id: str
    path: Path
    sha256: str
    size_bytes: int
    media_type: SupportedMediaType
    extension: str
    page_count: int
    width: int | None
    height: int | None
