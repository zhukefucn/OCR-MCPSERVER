from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ocr_mcp_server.api.contracts import (
    DocumentSource,
    OrientationReparseRequest,
    ParseDocumentsRequest,
)
from ocr_mcp_server.api.production_gateway import ProductionDocumentGateway
from ocr_mcp_server.domain.files import StoredFile, SupportedMediaType
from ocr_mcp_server.domain.models import (
    BatchStatus,
    FileStatus,
    ProcessingStage,
)
from ocr_mcp_server.domain.tasks import (
    BatchSnapshot,
    CreateBatchResult,
    FileTaskSnapshot,
)


NOW = datetime(2026, 7, 23, tzinfo=UTC)


def _stored() -> StoredFile:
    file_id = str(uuid4())
    return StoredFile(
        file_id=file_id,
        path=__file__,
        sha256="a" * 64,
        size_bytes=4,
        media_type=SupportedMediaType.PDF,
        extension=".pdf",
        page_count=1,
        width=None,
        height=None,
    )


@pytest.mark.asyncio
async def test_gateway_upload_replays_durable_idempotency_mapping() -> None:
    stored = _stored()
    snapshot = SimpleNamespace(
        file_id=stored.file_id,
        size_bytes=stored.size_bytes,
        media_type=stored.media_type,
    )

    class Uploads:
        async def get_by_idempotency_key(self, key):
            assert key == "upload-1"
            return snapshot

    gateway = ProductionDocumentGateway(
        intake=SimpleNamespace(),
        uploads=Uploads(),
        tasks=SimpleNamespace(),
        orchestration=SimpleNamespace(),
        artifacts=SimpleNamespace(),
        recovery=SimpleNamespace(),
    )

    async def content():
        yield b"pdf"

    receipt = await gateway.upload_document(
        content(),
        display_name="bank.pdf",
        media_type="application/pdf",
        content_length=3,
        idempotency_key="upload-1",
    )
    assert receipt.file_id == stored.file_id
    assert receipt.size_bytes == 4


@pytest.mark.asyncio
async def test_gateway_creates_durable_batch_notifies_and_projects_status() -> None:
    file_id = str(uuid4())
    batch_id = str(uuid4())
    upload = SimpleNamespace(file_id=file_id)
    batch = BatchSnapshot(
        id=batch_id,
        idempotency_key="parse-1",
        status=BatchStatus.QUEUED,
        total_files=1,
        completed_files=0,
        failed_files=0,
        cancelled_files=0,
        progress=12,
        created_at=NOW,
        updated_at=NOW,
        version=1,
        queued_files=1,
    )
    file = FileTaskSnapshot(
        id=file_id,
        batch_id=batch_id,
        position=0,
        status=FileStatus.QUEUED,
        stage=ProcessingStage.QUEUED,
        progress=12,
        attempt_count=0,
        max_attempts=3,
        lease_owner=None,
        lease_token=None,
        lease_expires_at=None,
        last_error_code=None,
        created_at=NOW,
        updated_at=NOW,
        version=1,
    )

    class Uploads:
        async def get(self, value):
            assert value == file_id
            return upload

    class Tasks:
        async def create_batch(self, key, file_ids, **kwargs):
            assert key == "parse-1"
            assert tuple(file_ids) == (file_id,)
            return CreateBatchResult(batch, (file,), True)

        async def get_batch(self, value):
            return batch if value == batch_id else None

        async def list_batch_files(self, value):
            return (file,) if value == batch_id else ()

    class Orchestration:
        def __init__(self):
            self.notifications = 0

        def notify_work(self):
            self.notifications += 1
            return True

    class Artifacts:
        async def list_for_batch(self, value):
            assert value == batch_id
            return ()

    orchestration = Orchestration()
    gateway = ProductionDocumentGateway(
        intake=SimpleNamespace(),
        uploads=Uploads(),
        tasks=Tasks(),
        orchestration=orchestration,
        artifacts=Artifacts(),
        recovery=SimpleNamespace(),
    )
    submitted = await gateway.parse_documents(
        ParseDocumentsRequest(
            sources=[DocumentSource(file_id=file_id)],
            idempotency_key="parse-1",
        )
    )
    status = await gateway.get_task_status(batch_id)

    assert submitted.batch_id == batch_id
    assert orchestration.notifications == 1
    assert status.batch_id == batch_id
    assert status.files[0].stage is ProcessingStage.QUEUED


@pytest.mark.asyncio
async def test_gateway_delegates_orientation_recovery() -> None:
    result_batch_id = str(uuid4())

    class Recovery:
        async def reparse(self, command, *, progress=None):
            assert command.pages == (2,)
            return SimpleNamespace(
                batch_id=result_batch_id, status=BatchStatus.QUEUED
            )

    gateway = ProductionDocumentGateway(
        intake=SimpleNamespace(),
        uploads=SimpleNamespace(),
        tasks=SimpleNamespace(),
        orchestration=SimpleNamespace(),
        artifacts=SimpleNamespace(),
        recovery=Recovery(),
    )
    result = await gateway.reparse_with_page_orientation(
        OrientationReparseRequest(
            recovery_token="token-123456789", pages=[2]
        )
    )
    assert result.batch_id == result_batch_id


@pytest.mark.asyncio
async def test_gateway_projects_internal_artifact_id_to_public_uuid() -> None:
    file_id = str(uuid4())
    batch_id = str(uuid4())
    batch = BatchSnapshot(
        id=batch_id,
        idempotency_key="done",
        status=BatchStatus.COMPLETED,
        total_files=1,
        completed_files=1,
        failed_files=0,
        cancelled_files=0,
        progress=100,
        created_at=NOW,
        updated_at=NOW,
        version=2,
    )
    file = FileTaskSnapshot(
        id=file_id,
        batch_id=batch_id,
        position=0,
        status=FileStatus.COMPLETED,
        stage=ProcessingStage.COMPLETED,
        progress=100,
        attempt_count=1,
        max_attempts=3,
        lease_owner=None,
        lease_token=None,
        lease_expires_at=None,
        last_error_code=None,
        created_at=NOW,
        updated_at=NOW,
        version=2,
    )

    class Tasks:
        async def get_batch(self, value):
            return batch

        async def list_batch_files(self, value):
            return (file,)

    class Artifacts:
        async def list_for_batch(self, value):
            return (
                SimpleNamespace(
                    artifact_id="artifact-" + "a" * 64,
                    available=True,
                    expires_at=NOW + timedelta(hours=1),
                ),
            )

    gateway = ProductionDocumentGateway(
        intake=SimpleNamespace(),
        uploads=SimpleNamespace(),
        tasks=Tasks(),
        orchestration=SimpleNamespace(),
        artifacts=Artifacts(),
        recovery=SimpleNamespace(),
    )
    status = await gateway.get_task_status(batch_id)
    assert str(uuid4()).count("-") == status.artifacts[0].artifact_id.count("-")
