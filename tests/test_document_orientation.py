from __future__ import annotations

from dataclasses import replace
import asyncio

import pytest

from ocr_mcp_server.domain.orientation import OrientationEvidence, OrientationFailure
from ocr_mcp_server.domain.secondary_ocr import OrthogonalAngle
from ocr_mcp_server.services.orientation_recovery import (
    OrientationDetectionRequest,
    ValidatedPageOrientationDetector,
)


class _Detector:
    def __init__(self, results: object) -> None:
        self.results = results
        self.requests: list[OrientationDetectionRequest] = []

    async def detect(self, request: OrientationDetectionRequest):
        self.requests.append(request)
        return self.results


def _request(*pages: int) -> OrientationDetectionRequest:
    return OrientationDetectionRequest(
        batch_id="9f690c16-c3c4-412c-90f9-fae499fae195",
        file_id="b7fd3693-c445-49fe-a86c-6f7d13d1c949",
        page_count=3,
        pages=pages,
    )


@pytest.mark.asyncio
async def test_detector_validates_complete_independent_decisions_and_threshold() -> None:
    backend = _Detector(
        (
            OrientationEvidence(2, OrthogonalAngle.DEG_0, 0.99, "upright"),
            OrientationEvidence(1, OrthogonalAngle.DEG_90, 0.80, "layout"),
        )
    )

    decisions = await ValidatedPageOrientationDetector(
        backend, credibility_threshold=0.80
    ).detect(_request(1, 2))

    assert [decision.page_number for decision in decisions] == [1, 2]
    assert decisions[0].angle is OrthogonalAngle.DEG_90
    assert decisions[0].credible is True
    assert decisions[1].credible is True
    assert backend.requests == [_request(1, 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "results",
    [
        (OrientationEvidence(1, OrthogonalAngle.DEG_90, 0.9, "layout"),),
        (
            OrientationEvidence(1, OrthogonalAngle.DEG_90, 0.9, "layout"),
            OrientationEvidence(1, OrthogonalAngle.DEG_180, 0.9, "layout"),
        ),
        (
            OrientationEvidence(1, OrthogonalAngle.DEG_90, 0.9, "layout"),
            OrientationEvidence(3, OrthogonalAngle.DEG_180, 0.9, "layout"),
        ),
        [OrientationEvidence(1, OrthogonalAngle.DEG_90, 0.9, "layout"), object()],
    ],
)
async def test_detector_rejects_missing_duplicate_extra_or_untyped_decisions(results) -> None:
    with pytest.raises(OrientationFailure) as caught:
        await ValidatedPageOrientationDetector(
            _Detector(results), credibility_threshold=0.8
        ).detect(_request(1, 2))

    assert caught.value.code == "orientation_request_invalid"
    assert not caught.value.__dict__.get("cause")


@pytest.mark.asyncio
async def test_detector_marks_below_threshold_decision_uncertain() -> None:
    evidence = OrientationEvidence(1, OrthogonalAngle.DEG_270, 0.799, "layout")
    decisions = await ValidatedPageOrientationDetector(
        _Detector((evidence,)), credibility_threshold=0.8
    ).detect(_request(1))
    assert decisions[0].credible is False


@pytest.mark.asyncio
async def test_detector_does_not_swallow_task_cancellation() -> None:
    class _CancelledDetector:
        async def detect(self, request):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await ValidatedPageOrientationDetector(
            _CancelledDetector(), credibility_threshold=0.8
        ).detect(_request(1))


def test_detection_request_rejects_paths_duplicates_and_page_overflow() -> None:
    with pytest.raises(TypeError):
        OrientationDetectionRequest(  # type: ignore[call-arg]
            batch_id="9f690c16-c3c4-412c-90f9-fae499fae195",
            file_id="b7fd3693-c445-49fe-a86c-6f7d13d1c949",
            page_count=3,
            pages=(1,),
            path="private.pdf",
        )
    with pytest.raises(ValueError):
        replace(_request(1), pages=(1, 1))
    with pytest.raises(ValueError):
        replace(_request(1), pages=(4,))
