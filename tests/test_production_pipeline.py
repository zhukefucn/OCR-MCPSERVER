from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ocr_mcp_server.domain.models import ProcessingStage
from ocr_mcp_server.domain.orientation import OrientationDecision
from ocr_mcp_server.domain.secondary_ocr import OrthogonalAngle
from ocr_mcp_server.services.orchestration import (
    PipelineCancellation,
    PipelineFileIdentity,
    PipelineResult,
)
from ocr_mcp_server.services.production_pipeline import ProductionFilePipeline


@pytest.mark.asyncio
async def test_pipeline_composes_existing_steps_in_order(tmp_path) -> None:
    calls: list[str] = []
    file_id = str(uuid4())
    batch_id = str(uuid4())
    upload = SimpleNamespace(
        file_id=file_id,
        storage_batch_id=str(uuid4()),
        page_count=1,
    )
    stored = SimpleNamespace(
        path=tmp_path / "input.pdf",
        media_type="application/pdf",
        extension=".pdf",
    )
    mineru_result = SimpleNamespace(file_task_id=file_id)
    candidates = SimpleNamespace(
        candidates=(
            SimpleNamespace(candidate_id="candidate-1"),
            SimpleNamespace(candidate_id="candidate-2"),
        )
    )

    class Uploads:
        async def get(self, value):
            calls.append("upload")
            return upload

    class Storage:
        async def resolve_stored(self, storage_batch_id, value, **kwargs):
            calls.append("storage")
            return stored

    class MinerU:
        async def parse(self, request, *, progress_callback=None):
            calls.append("mineru")
            return mineru_result

    class Paddle:
        async def recognize(self, candidate):
            calls.append(f"paddle:{candidate.candidate_id}")
            return SimpleNamespace(state="valid")

    class Packaging:
        async def run(self, *args, **kwargs):
            calls.append("package")

    class Progress:
        async def report(self, stage, counters=None):
            calls.append(f"progress:{stage.value}")

    def collect(*args, **kwargs):
        calls.append("collect")
        return candidates

    def merge(*args, **kwargs):
        calls.append("merge")
        return SimpleNamespace()

    pipeline = ProductionFilePipeline(
        uploads=Uploads(),
        storage=Storage(),
        mineru=MinerU(),
        paddle=Paddle(),
        packaging=Packaging(),
        data_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        max_file_size_bytes=30 * 1024 * 1024,
        max_image_pixels=100_000_000,
        structured_limits=SimpleNamespace(),
        result_retention_hours=24,
        collect_candidates=collect,
        merge_publication=merge,
        now_factory=lambda: datetime(2026, 7, 23, tzinfo=UTC),
    )
    result = await pipeline.run(
        PipelineFileIdentity(file_id, batch_id, 0, 1),
        Progress(),
        PipelineCancellation(),
    )

    assert result == PipelineResult.success()
    assert calls == [
        "upload",
        "storage",
        "progress:mineru_parsing",
        "mineru",
        "progress:collecting_images",
        "collect",
        "progress:classifying_images",
        "progress:recognizing_images",
        "paddle:candidate-1",
        "progress:recognizing_images",
        "paddle:candidate-2",
        "progress:recognizing_images",
        "progress:merging",
        "merge",
        "package",
    ]


@pytest.mark.asyncio
async def test_pipeline_persists_orientation_once_per_file_result(
    tmp_path,
) -> None:
    calls: list[str] = []
    file_id, batch_id = str(uuid4()), str(uuid4())
    upload = SimpleNamespace(
        file_id=file_id, storage_batch_id=str(uuid4()), page_count=2
    )
    stored = SimpleNamespace(
        path=tmp_path / "input.pdf",
        media_type="application/pdf",
        extension=".pdf",
    )

    class Assessments:
        claimed = False

        async def begin(self, **kwargs):
            if self.claimed:
                return False
            self.claimed = True
            calls.append("assessment:begin")
            return True

        async def complete(self, **kwargs):
            calls.append(f"assessment:complete:{kwargs['suspected_pages']}")

        async def fail(self, **kwargs):
            calls.append("assessment:fail")

    class Detector:
        async def detect(self, request):
            calls.append("orientation:detect")
            return (
                OrientationDecision(
                    1, OrthogonalAngle.DEG_0, 0.99, "paddle_orientation", True
                ),
                OrientationDecision(
                    2, OrthogonalAngle.DEG_90, 0.96, "paddle_orientation", True
                ),
            )

    class Uploads:
        async def get(self, value):
            return upload

    class Storage:
        async def resolve_stored(self, *args, **kwargs):
            return stored

    class MinerU:
        async def parse(self, request, *, progress_callback=None):
            return SimpleNamespace(file_task_id=file_id)

    class Paddle:
        async def recognize(self, candidate):
            return SimpleNamespace(state="valid")

    class Packaging:
        async def run(self, *args, **kwargs):
            calls.append("package")

    class Progress:
        async def report(self, stage, counters=None):
            calls.append(f"progress:{stage.value}")

    pipeline = ProductionFilePipeline(
        uploads=Uploads(),
        storage=Storage(),
        mineru=MinerU(),
        paddle=Paddle(),
        packaging=Packaging(),
        orientation_detector=Detector(),
        orientation_assessments=Assessments(),
        data_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        max_file_size_bytes=30 * 1024 * 1024,
        max_image_pixels=100_000_000,
        structured_limits=SimpleNamespace(),
        result_retention_hours=24,
        collect_candidates=lambda *args, **kwargs: SimpleNamespace(candidates=()),
        merge_publication=lambda *args, **kwargs: SimpleNamespace(),
        now_factory=lambda: datetime(2026, 7, 23, tzinfo=UTC),
    )
    identity = PipelineFileIdentity(file_id, batch_id, 0, 1)
    assert (
        await pipeline.run(identity, Progress(), PipelineCancellation())
        == PipelineResult.success()
    )
    assert (
        await pipeline.run(identity, Progress(), PipelineCancellation())
        == PipelineResult.success()
    )
    assert calls.count("orientation:detect") == 1
    assert "assessment:complete:(2,)" in calls
    assert calls.count("package") == 2


@pytest.mark.asyncio
async def test_orientation_failure_is_persisted_as_warning_and_artifact_still_packages(
    tmp_path,
) -> None:
    file_id, batch_id = str(uuid4()), str(uuid4())
    calls: list[str] = []

    class Assessments:
        async def begin(self, **kwargs):
            return True

        async def complete(self, **kwargs):
            raise AssertionError("failed evidence cannot complete")

        async def fail(self, **kwargs):
            assert kwargs["error_code"] == "orientation_detection_failed"
            calls.append("assessment:fail")

    class Detector:
        async def detect(self, request):
            raise RuntimeError("private content must not escape")

    class Packaging:
        async def run(self, *args, **kwargs):
            calls.append("package")

    pipeline = ProductionFilePipeline(
        uploads=SimpleNamespace(
            get=lambda *_: _async_value(
                SimpleNamespace(storage_batch_id=str(uuid4()), page_count=1)
            )
        ),
        storage=SimpleNamespace(
            resolve_stored=lambda *args, **kwargs: _async_value(
                SimpleNamespace(
                    path=tmp_path / "input.pdf",
                    media_type="application/pdf",
                    extension=".pdf",
                )
            )
        ),
        mineru=SimpleNamespace(
            parse=lambda *args, **kwargs: _async_value(
                SimpleNamespace(file_task_id=file_id)
            )
        ),
        paddle=SimpleNamespace(),
        packaging=Packaging(),
        orientation_detector=Detector(),
        orientation_assessments=Assessments(),
        data_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        max_file_size_bytes=30 * 1024 * 1024,
        max_image_pixels=100_000_000,
        structured_limits=SimpleNamespace(),
        result_retention_hours=24,
        collect_candidates=lambda *args, **kwargs: SimpleNamespace(candidates=()),
        merge_publication=lambda *args, **kwargs: SimpleNamespace(),
        now_factory=lambda: datetime(2026, 7, 23, tzinfo=UTC),
    )

    result = await pipeline.run(
        PipelineFileIdentity(file_id, batch_id, 0, 1),
        SimpleNamespace(report=lambda *args, **kwargs: _async_value(None)),
        PipelineCancellation(),
    )

    assert result == PipelineResult.success_with_warnings()
    assert calls == ["assessment:fail", "package"]


async def _async_value(value):
    return value
