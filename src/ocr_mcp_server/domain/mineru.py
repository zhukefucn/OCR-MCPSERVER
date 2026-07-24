"""Transport-independent contracts for one MinerU file task."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class MinerUProgressStatus(StrEnum):
    """Non-terminal upstream states exposed to progress observers."""

    PENDING = "pending"
    PROCESSING = "processing"


@dataclass(frozen=True, slots=True)
class MinerUParseRequest:
    """Trusted local inputs for a MinerU parse operation."""

    file_task_id: str
    source_path: Path
    upload_name: str
    output_directory: Path


@dataclass(frozen=True, slots=True)
class MinerUSubmission:
    """Validated upstream task endpoints bound to the local file task."""

    file_task_id: str
    upstream_task_id: str
    status_url: str
    result_url: str
    queued_ahead: int | None = None


@dataclass(frozen=True, slots=True)
class MinerUProgress:
    """A pending or processing status update for one local file task."""

    file_task_id: str
    upstream_task_id: str
    status: MinerUProgressStatus
    queued_ahead: int | None = None


@dataclass(frozen=True, slots=True)
class MinerUDocumentResult:
    """Normalized paths for a completely published MinerU result."""

    file_task_id: str
    upstream_task_id: str
    result_root: Path
    markdown_path: Path | None
    middle_json_path: Path | None
    content_list_v2_path: Path
    legacy_content_list_path: Path | None
    images_directory: Path
