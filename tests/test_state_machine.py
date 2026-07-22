from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ocr_mcp_server.domain.errors import StateTransitionError
from ocr_mcp_server.domain.models import BatchStatus, FileStatus, ProcessingStage
from ocr_mcp_server.domain.state_machine import (
    aggregate_batch_status,
    validate_file_transition,
)
from ocr_mcp_server.domain.tasks import BatchSnapshot, FileTaskSnapshot


def test_legal_processing_progress_and_terminal_transition() -> None:
    validate_file_transition(
        FileStatus.QUEUED,
        ProcessingStage.QUEUED,
        0,
        FileStatus.PROCESSING,
        ProcessingStage.QUEUED,
        0,
    )
    validate_file_transition(
        FileStatus.PROCESSING,
        ProcessingStage.QUEUED,
        0,
        FileStatus.PROCESSING,
        ProcessingStage.MINERU_PARSING,
        35,
    )
    validate_file_transition(
        FileStatus.PROCESSING,
        ProcessingStage.MINERU_PARSING,
        35,
        FileStatus.COMPLETED,
        ProcessingStage.COMPLETED,
        100,
    )


@pytest.mark.parametrize(
    ("old_status", "old_stage", "old_progress", "status", "stage", "progress"),
    [
        (FileStatus.QUEUED, ProcessingStage.QUEUED, 0, FileStatus.COMPLETED, ProcessingStage.COMPLETED, 100),
        (FileStatus.PROCESSING, ProcessingStage.MERGING, 70, FileStatus.PROCESSING, ProcessingStage.RECOGNIZING_IMAGES, 80),
        (FileStatus.PROCESSING, ProcessingStage.MERGING, 70, FileStatus.PROCESSING, ProcessingStage.PACKAGING, 69),
        (FileStatus.PROCESSING, ProcessingStage.MERGING, 70, FileStatus.COMPLETED, ProcessingStage.COMPLETED, 99),
        (FileStatus.PROCESSING, ProcessingStage.MERGING, 70, FileStatus.FAILED, ProcessingStage.COMPLETED, 100),
        (FileStatus.COMPLETED, ProcessingStage.COMPLETED, 100, FileStatus.COMPLETED, ProcessingStage.COMPLETED, 100),
        (FileStatus.PROCESSING, ProcessingStage.MERGING, 70, FileStatus.QUEUED, ProcessingStage.QUEUED, 70),
    ],
)
def test_invalid_status_stage_progress_and_terminal_changes_are_rejected(
    old_status: FileStatus,
    old_stage: ProcessingStage,
    old_progress: int,
    status: FileStatus,
    stage: ProcessingStage,
    progress: int,
) -> None:
    with pytest.raises(StateTransitionError) as exc_info:
        validate_file_transition(
            old_status, old_stage, old_progress, status, stage, progress
        )

    assert exc_info.value.code == "state_transition_invalid"
    assert str(exc_info.value) == "Task state transition is invalid."


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([FileStatus.PROCESSING, FileStatus.QUEUED], BatchStatus.PROCESSING),
        ([FileStatus.QUEUED, FileStatus.COMPLETED], BatchStatus.QUEUED),
        ([FileStatus.COMPLETED, FileStatus.COMPLETED_WITH_WARNINGS], BatchStatus.COMPLETED),
        ([FileStatus.FAILED, FileStatus.FAILED], BatchStatus.FAILED),
        ([FileStatus.CANCELLED, FileStatus.CANCELLED], BatchStatus.CANCELLED),
        ([FileStatus.COMPLETED, FileStatus.FAILED], BatchStatus.COMPLETED_WITH_ERRORS),
        ([FileStatus.FAILED, FileStatus.CANCELLED], BatchStatus.COMPLETED_WITH_ERRORS),
    ],
)
def test_batch_status_aggregation(statuses: list[FileStatus], expected: BatchStatus) -> None:
    assert aggregate_batch_status(statuses) is expected


def test_task_snapshots_are_frozen_and_keep_aware_utc_times() -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    batch = BatchSnapshot(
        id="batch-1",
        idempotency_key="digest-1",
        status=BatchStatus.QUEUED,
        total_files=1,
        completed_files=0,
        failed_files=0,
        cancelled_files=0,
        progress=0,
        created_at=now,
        updated_at=now,
        version=1,
    )
    file = FileTaskSnapshot(
        id="file-1",
        batch_id="batch-1",
        position=0,
        status=FileStatus.QUEUED,
        stage=ProcessingStage.QUEUED,
        progress=0,
        attempt_count=0,
        max_attempts=3,
        lease_owner=None,
        lease_token=None,
        lease_expires_at=None,
        last_error_code=None,
        created_at=now,
        updated_at=now,
        version=1,
    )

    assert batch.created_at.tzinfo is UTC
    assert file.updated_at.tzinfo is UTC
    with pytest.raises(AttributeError):
        file.progress = 50  # type: ignore[misc]
