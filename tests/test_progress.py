from __future__ import annotations

import math

import pytest

from ocr_mcp_server.domain.errors import InputValidationError
from ocr_mcp_server.domain.models import ProcessingStage
from ocr_mcp_server.domain.progress import (
    ProgressCounters,
    ProgressUnit,
    STAGE_PROGRESS_RANGES,
    map_stage_progress,
)


EXPECTED_RANGES = {
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


def test_stage_ranges_and_integer_mapping_are_exact_and_bounded() -> None:
    assert STAGE_PROGRESS_RANGES == EXPECTED_RANGES

    for stage, (start, end) in EXPECTED_RANGES.items():
        if start == end:
            assert map_stage_progress(stage) == start
            continue
        assert map_stage_progress(stage) == start
        assert map_stage_progress(
            stage,
            ProgressCounters(0, 3, ProgressUnit.ITEMS),
        ) == start
        assert map_stage_progress(
            stage,
            ProgressCounters(1, 3, ProgressUnit.ITEMS),
        ) == start + (end - start) // 3
        assert map_stage_progress(
            stage,
            ProgressCounters(3, 3, ProgressUnit.ITEMS),
        ) == end


def test_unknown_and_zero_totals_use_the_stage_start() -> None:
    assert map_stage_progress(
        ProcessingStage.MINERU_PARSING,
        ProgressCounters(17, None, ProgressUnit.PAGES),
    ) == 12
    assert map_stage_progress(
        ProcessingStage.RECOGNIZING_IMAGES,
        ProgressCounters(0, 0, ProgressUnit.IMAGES),
    ) == 75


@pytest.mark.parametrize(
    "completed,total,unit",
    [
        (True, 1, ProgressUnit.ITEMS),
        (0, True, ProgressUnit.ITEMS),
        (-1, 1, ProgressUnit.ITEMS),
        (2, 1, ProgressUnit.ITEMS),
        (1, 0, ProgressUnit.ITEMS),
        (math.nan, 1, ProgressUnit.ITEMS),
        (0, math.inf, ProgressUnit.ITEMS),
        (0, 1, "widgets"),
        (None, 1, ProgressUnit.ITEMS),
        (0, None, None),
    ],
)
def test_invalid_or_incomplete_counters_are_rejected(
    completed: object, total: object, unit: object
) -> None:
    with pytest.raises(InputValidationError):
        ProgressCounters(completed, total, unit)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "stage",
    [
        ProcessingStage.QUEUED,
        ProcessingStage.COMPLETED,
        ProcessingStage.COMPLETED_WITH_WARNINGS,
        ProcessingStage.COMPLETED_WITH_ERRORS,
        ProcessingStage.FAILED,
        ProcessingStage.CANCELLED,
    ],
)
def test_counters_are_rejected_for_queued_and_terminal_stages(
    stage: ProcessingStage,
) -> None:
    with pytest.raises(InputValidationError):
        map_stage_progress(stage, ProgressCounters(1, 2, ProgressUnit.ITEMS))


def test_unknown_stage_is_rejected_without_leaking_input() -> None:
    sensitive = "C:/private/customer-contract.pdf"
    with pytest.raises(InputValidationError) as exc_info:
        map_stage_progress(sensitive)  # type: ignore[arg-type]

    assert sensitive not in str(exc_info.value)
