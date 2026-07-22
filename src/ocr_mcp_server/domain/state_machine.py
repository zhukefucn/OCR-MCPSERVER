"""Pure task transition validation and batch-status aggregation."""

from __future__ import annotations

from collections.abc import Sequence

from .errors import StateTransitionError
from .models import BatchStatus, FileStatus, ProcessingStage

_TERMINAL_STATUSES = frozenset(
    {
        FileStatus.COMPLETED,
        FileStatus.COMPLETED_WITH_WARNINGS,
        FileStatus.FAILED,
        FileStatus.CANCELLED,
    }
)
_SUCCESS_STATUSES = frozenset(
    {FileStatus.COMPLETED, FileStatus.COMPLETED_WITH_WARNINGS}
)
_ALLOWED_STATUS_TRANSITIONS = {
    FileStatus.QUEUED: frozenset({FileStatus.PROCESSING, FileStatus.CANCELLED}),
    FileStatus.PROCESSING: frozenset(
        {
            FileStatus.COMPLETED,
            FileStatus.COMPLETED_WITH_WARNINGS,
            FileStatus.FAILED,
            FileStatus.CANCELLED,
        }
    ),
}
_STAGE_ORDER = {
    stage: position
    for position, stage in enumerate(
        (
            ProcessingStage.UPLOADING,
            ProcessingStage.VALIDATING,
            ProcessingStage.QUEUED,
            ProcessingStage.MINERU_PARSING,
            ProcessingStage.COLLECTING_IMAGES,
            ProcessingStage.DETECTING_ORIENTATION,
            ProcessingStage.CLASSIFYING_IMAGES,
            ProcessingStage.RECOGNIZING_IMAGES,
            ProcessingStage.MERGING,
            ProcessingStage.PACKAGING,
            ProcessingStage.PUBLISHING,
        )
    )
}
_TERMINAL_STAGE = {
    FileStatus.COMPLETED: ProcessingStage.COMPLETED,
    FileStatus.COMPLETED_WITH_WARNINGS: ProcessingStage.COMPLETED_WITH_WARNINGS,
    FileStatus.FAILED: ProcessingStage.FAILED,
    FileStatus.CANCELLED: ProcessingStage.CANCELLED,
}


def validate_file_transition(
    old_status: FileStatus,
    old_stage: ProcessingStage,
    old_progress: int,
    status: FileStatus,
    stage: ProcessingStage,
    progress: int,
) -> None:
    """Raise a safe domain error unless a file transition is valid."""

    if not 0 <= progress <= 100 or not 0 <= old_progress <= 100:
        raise StateTransitionError()
    if old_status in _TERMINAL_STATUSES:
        raise StateTransitionError()
    if status != old_status and status not in _ALLOWED_STATUS_TRANSITIONS[old_status]:
        raise StateTransitionError()
    if status is FileStatus.QUEUED and old_status is FileStatus.PROCESSING:
        raise StateTransitionError()
    if progress < old_progress:
        raise StateTransitionError()
    if status in _TERMINAL_STATUSES:
        if progress != 100 or stage is not _TERMINAL_STAGE[status]:
            raise StateTransitionError()
        return
    if stage not in _STAGE_ORDER or old_stage not in _STAGE_ORDER:
        raise StateTransitionError()
    if _STAGE_ORDER[stage] < _STAGE_ORDER[old_stage]:
        raise StateTransitionError()
    if progress == 100:
        raise StateTransitionError()


def aggregate_batch_status(statuses: Sequence[FileStatus]) -> BatchStatus:
    """Derive a batch status from its file statuses."""

    if not statuses:
        raise StateTransitionError()
    if any(status is FileStatus.PROCESSING for status in statuses):
        return BatchStatus.PROCESSING
    if any(status not in _TERMINAL_STATUSES for status in statuses):
        return BatchStatus.QUEUED
    if all(status in _SUCCESS_STATUSES for status in statuses):
        return BatchStatus.COMPLETED
    if all(status is FileStatus.FAILED for status in statuses):
        return BatchStatus.FAILED
    if all(status is FileStatus.CANCELLED for status in statuses):
        return BatchStatus.CANCELLED
    return BatchStatus.COMPLETED_WITH_ERRORS
