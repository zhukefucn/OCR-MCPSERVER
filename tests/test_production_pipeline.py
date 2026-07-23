from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ocr_mcp_server.domain.models import ProcessingStage
from ocr_mcp_server.domain.orientation import OrientationDecision
from ocr_mcp_server.domain.secondary_ocr import OrthogonalAngle
from ocr_mcp_server.domain.secondary_ocr import OrientationClassificationResult
from ocr_mcp_server.infra.secondary_ocr import SingleOwnerSecondaryOcrWorker
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
        claim = SimpleNamespace(claim_token="claim-1")

        async def begin(self, **kwargs):
            if self.claimed:
                return None
            self.claimed = True
            calls.append("assessment:begin")
            return self.claim

        async def complete(self, claim, **kwargs):
            assert claim is self.claim
            calls.append(f"assessment:complete:{kwargs['suspected_pages']}")

        async def fail(self, claim, **kwargs):
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
        orientation_assessment_timeout_seconds=0.2,
        orientation_assessment_lease_seconds=1,
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
    assert calls.index("package") < calls.index("orientation:detect")


@pytest.mark.asyncio
async def test_orientation_failure_is_persisted_as_warning_and_artifact_still_packages(
    tmp_path,
) -> None:
    file_id, batch_id = str(uuid4()), str(uuid4())
    calls: list[str] = []

    class Assessments:
        claim = SimpleNamespace(claim_token="claim-1")

        async def begin(self, **kwargs):
            return self.claim

        async def complete(self, claim, **kwargs):
            raise AssertionError("failed evidence cannot complete")

        async def fail(self, claim, **kwargs):
            assert claim is self.claim
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
        orientation_assessment_timeout_seconds=0.2,
        orientation_assessment_lease_seconds=1,
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
    assert calls == ["package", "assessment:fail"]


@pytest.mark.asyncio
async def test_hung_orientation_classifier_times_out_after_artifact_publication(
    tmp_path,
) -> None:
    file_id, batch_id = str(uuid4()), str(uuid4())
    calls: list[str] = []
    classifier_cancelled = asyncio.Event()

    class Assessments:
        claim = SimpleNamespace(claim_token="claim-timeout")

        async def begin(self, **kwargs):
            calls.append("assessment:begin")
            return self.claim

        async def complete(self, claim, **kwargs):
            raise AssertionError("timed out evidence cannot complete")

        async def fail(self, claim, **kwargs):
            assert claim is self.claim
            assert kwargs["error_code"] == "orientation_detection_timeout"
            calls.append("assessment:timeout")

    class Detector:
        async def detect(self, request):
            calls.append("orientation:detect")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                classifier_cancelled.set()
                raise

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
        orientation_assessment_timeout_seconds=0.05,
        orientation_assessment_lease_seconds=1,
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

    started = asyncio.get_running_loop().time()
    result = await pipeline.run(
        PipelineFileIdentity(file_id, batch_id, 0, 1),
        SimpleNamespace(report=lambda *args, **kwargs: _async_value(None)),
        PipelineCancellation(),
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert result == PipelineResult.success_with_warnings()
    assert elapsed < 0.5
    assert classifier_cancelled.is_set()
    assert calls == [
        "package",
        "assessment:begin",
        "orientation:detect",
        "assessment:timeout",
    ]


@pytest.mark.asyncio
async def test_timeout_detaches_sync_paddle_job_and_runtime_close_remains_safe(
    tmp_path,
) -> None:
    entered, release = threading.Event(), threading.Event()
    file_id, batch_id = str(uuid4()), str(uuid4())

    class Backend:
        def recognize(self, candidate):
            raise AssertionError("not used")

        def classify_orientation(self, candidate):
            entered.set()
            assert release.wait(5)
            return OrientationClassificationResult(
                OrthogonalAngle.DEG_90, 0.99, "trusted"
            )

        def close(self):
            return None

    worker = SingleOwnerSecondaryOcrWorker(lambda: Backend(), queue_capacity=1)
    await worker.start()

    class Detector:
        async def detect(self, request):
            await worker.classify_orientation(SimpleNamespace())
            return ()

    claim = SimpleNamespace(claim_token="claim-sync")

    class Assessments:
        async def begin(self, **kwargs):
            return claim

        async def complete(self, actual, **kwargs):
            raise AssertionError("timed out result cannot complete")

        async def fail(self, actual, **kwargs):
            assert actual is claim

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
        packaging=SimpleNamespace(
            run=lambda *args, **kwargs: _async_value(None)
        ),
        orientation_detector=Detector(),
        orientation_assessments=Assessments(),
        orientation_assessment_timeout_seconds=0.05,
        orientation_assessment_lease_seconds=1,
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
    assert entered.is_set()
    assert worker.owner_thread_alive is True

    release.set()
    await asyncio.wait_for(worker.close(), timeout=1)
    assert worker.owner_thread_alive is False


async def _async_value(value):
    return value
