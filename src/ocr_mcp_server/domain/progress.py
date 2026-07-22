"""Pure, content-free progress values and weighted display mapping."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .errors import InputValidationError
from .models import ProcessingStage


class ProgressUnit(StrEnum):
    BYTES = "bytes"
    PAGES = "pages"
    IMAGES = "images"
    ITEMS = "items"


STAGE_PROGRESS_RANGES: dict[ProcessingStage, tuple[int, int]] = {
    ProcessingStage.UPLOADING: (0, 10),
    ProcessingStage.VALIDATING: (10, 12),
    ProcessingStage.QUEUED: (12, 12),
    ProcessingStage.MINERU_PARSING: (12, 60),
    ProcessingStage.COLLECTING_IMAGES: (60, 63),
    ProcessingStage.DETECTING_ORIENTATION: (63, 68),
    ProcessingStage.CLASSIFYING_IMAGES: (68, 75),
    ProcessingStage.RECOGNIZING_IMAGES: (75, 88),
    ProcessingStage.MERGING: (88, 94),
    ProcessingStage.PACKAGING: (94, 97),
    ProcessingStage.PUBLISHING: (97, 99),
    ProcessingStage.COMPLETED: (100, 100),
    ProcessingStage.COMPLETED_WITH_WARNINGS: (100, 100),
    ProcessingStage.COMPLETED_WITH_ERRORS: (100, 100),
    ProcessingStage.FAILED: (100, 100),
    ProcessingStage.CANCELLED: (100, 100),
}

_COUNTERLESS_STAGES = frozenset(
    stage for stage, (start, end) in STAGE_PROGRESS_RANGES.items() if start == end
)


@dataclass(frozen=True, slots=True)
class ProgressCounters:
    """Exact durable work counters; ``None`` total means not yet known."""

    completed_units: int
    total_units: int | None
    unit: ProgressUnit

    def __post_init__(self) -> None:
        if (
            isinstance(self.completed_units, bool)
            or not isinstance(self.completed_units, int)
            or self.completed_units < 0
            or isinstance(self.total_units, bool)
            or (
                self.total_units is not None
                and (
                    not isinstance(self.total_units, int)
                    or self.total_units < 0
                    or self.completed_units > self.total_units
                )
            )
            or not isinstance(self.unit, ProgressUnit)
        ):
            raise InputValidationError()


def map_stage_progress(
    stage: ProcessingStage,
    counters: ProgressCounters | None = None,
) -> int:
    """Map exact work counters into the fixed display range using integers."""

    if not isinstance(stage, ProcessingStage):
        raise InputValidationError()
    start, end = STAGE_PROGRESS_RANGES[stage]
    if counters is None:
        return start
    if not isinstance(counters, ProgressCounters) or stage in _COUNTERLESS_STAGES:
        raise InputValidationError()
    if counters.total_units in (None, 0):
        return start
    return min(
        end,
        start
        + (end - start) * counters.completed_units // counters.total_units,
    )
