from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ocr_mcp_server.domain.models import ProcessingStage
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
