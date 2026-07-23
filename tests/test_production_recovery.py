from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pypdf import PdfWriter

from ocr_mcp_server.domain.files import StoredFile, SupportedMediaType
from ocr_mcp_server.domain.models import BatchStatus
from ocr_mcp_server.domain.orientation import OrthogonalAngle
from ocr_mcp_server.services.orientation_recovery import OrientationDetectionRequest
from ocr_mcp_server.services.production_recovery import (
    MappedRecoveryStorage,
    ProductionPageOrientationDetector,
    RecoveryPipelineRunner,
)
from PIL import Image
from ocr_mcp_server.domain.secondary_ocr import OrientationClassificationResult


@pytest.mark.asyncio
async def test_metadata_detector_normalizes_nonzero_pdf_rotation(tmp_path: Path) -> None:
    path = tmp_path / "rotated.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    page.rotate(90)
    with path.open("wb") as output:
        writer.write(output)
    file_id = str(uuid4())
    storage_batch_id = str(uuid4())
    upload = SimpleNamespace(
        file_id=file_id, storage_batch_id=storage_batch_id, page_count=1
    )

    class Uploads:
        async def get(self, value):
            return upload if value == file_id else None

    class Storage:
        async def resolve_stored(self, *args, **kwargs):
            return StoredFile(
                file_id=file_id,
                path=path,
                sha256="a" * 64,
                size_bytes=path.stat().st_size,
                media_type=SupportedMediaType.PDF,
                extension=".pdf",
                page_count=1,
                width=None,
                height=None,
            )

    class Paddle:
        async def recognize(self, candidate):
            raise AssertionError("PDF metadata must be preferred")

    evidence = await ProductionPageOrientationDetector(
        Uploads(),
        Storage(),
        Paddle(),
        max_file_size_bytes=30 * 1024 * 1024,
        max_image_pixels=1_000_000,
    ).detect(
        OrientationDetectionRequest(str(uuid4()), file_id, 1, (1,))
    )
    assert evidence[0].angle is OrthogonalAngle.DEG_270
    assert evidence[0].confidence == 1.0


@pytest.mark.asyncio
async def test_image_orientation_uses_dedicated_classifier_confidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rotated.png"
    Image.new("RGB", (4, 8), "white").save(path)
    file_id = str(uuid4())
    upload = SimpleNamespace(
        file_id=file_id, storage_batch_id=str(uuid4()), page_count=1
    )
    stored = StoredFile(
        file_id=file_id,
        path=path,
        sha256="a" * 64,
        size_bytes=path.stat().st_size,
        media_type=SupportedMediaType.PNG,
        extension=".png",
        page_count=1,
        width=4,
        height=8,
    )

    class Uploads:
        async def get(self, value):
            return upload

    class Storage:
        async def resolve_stored(self, *args, **kwargs):
            return stored

    class Paddle:
        async def classify_orientation(self, candidate):
            assert candidate.primary_path == path
            return OrientationClassificationResult(
                angle=OrthogonalAngle.DEG_90,
                confidence=0.91,
                model_version="trusted",
            )

    evidence = await ProductionPageOrientationDetector(
        Uploads(),
        Storage(),
        Paddle(),
        max_file_size_bytes=30 * 1024 * 1024,
        max_image_pixels=100,
    ).detect(OrientationDetectionRequest(str(uuid4()), file_id, 1, (1,)))
    assert evidence[0].angle is OrthogonalAngle.DEG_90
    assert evidence[0].confidence == 0.91
    assert evidence[0].evidence_code == "paddle_orientation"


@pytest.mark.asyncio
async def test_uncertain_paddle_result_is_zero_confidence(tmp_path: Path) -> None:
    path = tmp_path / "uncertain.jpg"
    Image.new("RGB", (4, 4), "white").save(path)
    file_id = str(uuid4())
    upload = SimpleNamespace(
        file_id=file_id, storage_batch_id=str(uuid4()), page_count=1
    )
    stored = StoredFile(
        file_id=file_id,
        path=path,
        sha256="a" * 64,
        size_bytes=path.stat().st_size,
        media_type=SupportedMediaType.JPEG,
        extension=".jpg",
        page_count=1,
        width=4,
        height=4,
    )

    class Paddle:
        async def classify_orientation(self, candidate):
            return OrientationClassificationResult(
                angle=OrthogonalAngle.DEG_0,
                confidence=0.0,
                model_version="trusted",
            )

    detector = ProductionPageOrientationDetector(
        SimpleNamespace(get=lambda _: upload),
        SimpleNamespace(resolve_stored=lambda *args, **kwargs: stored),
        Paddle(),
        max_file_size_bytes=30 * 1024 * 1024,
        max_image_pixels=100,
    )
    # Async protocol fakes remain explicit.
    async def get(_):
        return upload

    async def resolve(*args, **kwargs):
        return stored

    detector._uploads.get = get
    detector._storage.resolve_stored = resolve
    evidence = await detector.detect(
        OrientationDetectionRequest(str(uuid4()), file_id, 1, (1,))
    )
    assert evidence[0].angle is OrthogonalAngle.DEG_0
    assert evidence[0].confidence == 0.0


@pytest.mark.asyncio
async def test_recovery_runner_durably_adopts_and_notifies() -> None:
    corrected_id = str(uuid4())
    corrected = SimpleNamespace(
        file_id=corrected_id,
        path=Path(__file__),
        extension=".pdf",
        media_type=SupportedMediaType.PDF,
        sha256="b" * 64,
        size_bytes=10,
    )
    accepted = SimpleNamespace(
        file_id=str(uuid4()),
        sha256=corrected.sha256,
        size_bytes=corrected.size_bytes,
        adopted_source_file_id=corrected_id,
        result_version=3,
    )
    batch_id = str(uuid4())

    class Intake:
        async def ingest_upload(self, storage_batch_id, incoming):
            assert storage_batch_id != batch_id
            async for _ in incoming.content:
                pass
            return accepted

    class Uploads:
        async def register(self, stored, **kwargs):
            assert kwargs["adopted_source_file_id"] == corrected_id
            assert kwargs["result_version"] == 3
            return accepted

        async def get(self, file_id):
            return accepted if file_id == accepted.file_id else None

    class Tasks:
        async def create_batch(self, key, file_ids, **kwargs):
            assert key == "recovery:claim-1"
            assert kwargs["require_available_uploads_at"] == datetime(
                2026, 7, 23, tzinfo=UTC
            )
            return SimpleNamespace(
                batch=SimpleNamespace(id=batch_id, status=BatchStatus.QUEUED),
                files=(SimpleNamespace(id=accepted.file_id),),
                created=True,
            )

    class Orchestration:
        def __init__(self):
            self.calls = 0

        def notify_work(self):
            self.calls += 1

    orchestration = Orchestration()
    runner = RecoveryPipelineRunner(
        intake=Intake(),
        uploads=Uploads(),
        tasks=Tasks(),
        orchestration=orchestration,
        now_factory=lambda: datetime(2026, 7, 23, tzinfo=UTC),
        retention_hours=24,
    )
    submission = await runner.run(
        corrected,
        recovery_id="claim-1",
        source_batch_id=str(uuid4()),
        source_result_version=2,
        corrected_input_version=3,
    )
    assert submission.batch_id == batch_id
    assert submission.accepted_input_file_id == accepted.file_id
    assert orchestration.calls == 1


@pytest.mark.asyncio
async def test_recovery_runner_replay_returns_the_batch_owned_upload() -> None:
    corrected_id = str(uuid4())
    corrected = SimpleNamespace(
        file_id=corrected_id,
        path=Path(__file__),
        extension=".pdf",
        media_type=SupportedMediaType.PDF,
        sha256="b" * 64,
        size_bytes=10,
    )
    losing = SimpleNamespace(
        file_id=str(uuid4()),
        sha256=corrected.sha256,
        size_bytes=corrected.size_bytes,
    )
    winning = SimpleNamespace(
        file_id=str(uuid4()),
        sha256="c" * 64,
        size_bytes=11,
        adopted_source_file_id=corrected_id,
        result_version=3,
    )
    batch_id = str(uuid4())

    class Intake:
        async def ingest_upload(self, storage_batch_id, incoming):
            async for _ in incoming.content:
                pass
            return losing

    class Uploads:
        async def register(self, stored, **kwargs):
            return losing

        async def get(self, file_id):
            return winning if file_id == winning.file_id else None

    class Tasks:
        async def create_batch(self, key, file_ids, **kwargs):
            return SimpleNamespace(
                batch=SimpleNamespace(id=batch_id, status=BatchStatus.QUEUED),
                files=(SimpleNamespace(id=winning.file_id),),
                created=False,
            )

    class Orchestration:
        def notify_work(self):
            raise AssertionError("an idempotent replay must not notify")

    runner = RecoveryPipelineRunner(
        intake=Intake(),
        uploads=Uploads(),
        tasks=Tasks(),
        orchestration=Orchestration(),
        now_factory=lambda: datetime(2026, 7, 23, tzinfo=UTC),
        retention_hours=24,
    )

    submission = await runner.run(
        corrected,
        recovery_id="claim-1",
        source_batch_id=str(uuid4()),
        source_result_version=2,
        corrected_input_version=3,
    )

    assert submission.batch_id == batch_id
    assert submission.accepted_input_file_id == winning.file_id
    assert submission.accepted_input_sha256 == winning.sha256
    assert submission.accepted_input_size_bytes == winning.size_bytes


@pytest.mark.asyncio
async def test_mapped_recovery_storage_acquires_unique_roots_canonically() -> None:
    first_file = SimpleNamespace(id=str(uuid4()))
    second_file = SimpleNamespace(id=str(uuid4()))
    third_file = SimpleNamespace(id=str(uuid4()))
    low = str(uuid4())
    high = str(uuid4())
    if low > high:
        low, high = high, low
    mappings = {
        first_file.id: SimpleNamespace(storage_batch_id=high),
        second_file.id: SimpleNamespace(storage_batch_id=low),
        third_file.id: SimpleNamespace(storage_batch_id=high),
    }
    acquired: list[str] = []

    class Tasks:
        async def list_batch_files(self, batch_id):
            return first_file, second_file, third_file

    class Uploads:
        async def get(self, file_id):
            return mappings[file_id]

    class Storage:
        def batch_lock(self, storage_batch_id, **kwargs):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def held():
                acquired.append(storage_batch_id)
                yield object()

            return held()

    mapped = MappedRecoveryStorage(Uploads(), Tasks(), Storage())
    async with mapped.batch_lock(
        str(uuid4()), marker_registry=object()
    ) as leases:
        assert set(leases) == {low, high}

    assert acquired == [low, high]
