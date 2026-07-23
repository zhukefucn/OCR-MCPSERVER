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
from ocr_mcp_server.api.gateway import GatewayConflict
from ocr_mcp_server.domain.orientation import RecoveryTokenBinding
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
async def test_upload_idempotency_race_discards_loser_content() -> None:
    winner, loser = _stored(), _stored()
    discarded = []

    class Intake:
        async def ingest_upload(self, *args):
            return loser

        async def discard_upload(self, storage_batch_id):
            discarded.append(storage_batch_id)

    class Uploads:
        async def get_by_idempotency_key(self, key):
            return None

        async def register(self, stored, **kwargs):
            return SimpleNamespace(
                file_id=winner.file_id, size_bytes=winner.size_bytes,
                media_type=winner.media_type,
            )

    gateway = ProductionDocumentGateway(
        intake=Intake(), uploads=Uploads(), tasks=SimpleNamespace(),
        orchestration=SimpleNamespace(), artifacts=SimpleNamespace(),
        recovery=SimpleNamespace(), id_factory=lambda: "storage-race",
    )
    async def content():
        yield b"%PDF-1.4"
    await gateway.upload_document(
        content(), display_name="a.pdf",
        media_type="application/pdf", content_length=8,
        idempotency_key="upload-race",
    )
    assert discarded == ["storage-race"]


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
            assert kwargs["require_available_uploads_at"] == NOW
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
        now_factory=lambda: NOW,
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
                SimpleNamespace(
                    artifact_id="artifact-" + "b" * 64,
                    available=True,
                    expires_at=NOW,
                ),
            )

    gateway = ProductionDocumentGateway(
        intake=SimpleNamespace(),
        uploads=SimpleNamespace(),
        tasks=Tasks(),
        orchestration=SimpleNamespace(),
        artifacts=Artifacts(),
        recovery=SimpleNamespace(),
        now_factory=lambda: NOW,
    )
    status = await gateway.get_task_status(batch_id)
    assert len(status.artifacts) == 1
    assert str(uuid4()).count("-") == status.artifacts[0].artifact_id.count("-")
    assert str(status.artifacts[0].download_url).endswith(
        f"/{status.artifacts[0].artifact_id}"
    )


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_parse_idempotency_replays_before_remote_fetch_and_rejects_mismatch() -> None:
    batch_id = str(uuid4())
    request = ParseDocumentsRequest(
        sources=[DocumentSource(url="https://files.example.test/a.pdf")],
        idempotency_key="parse-remote",
    )

    class Tasks:
        async def get_by_idempotency_key(self, key):
            return SimpleNamespace(
                batch=SimpleNamespace(id=batch_id, status=BatchStatus.QUEUED),
                source_fingerprint=ProductionDocumentGateway.source_fingerprint(
                    request.sources
                ),
            )

    class Intake:
        async def ingest_remote(self, *args):
            raise AssertionError("idempotent replay must not fetch")

    gateway = ProductionDocumentGateway(
        intake=Intake(), uploads=SimpleNamespace(), tasks=Tasks(),
        orchestration=SimpleNamespace(), artifacts=SimpleNamespace(),
        recovery=SimpleNamespace(), remote_fetcher=object(),
    )
    assert (await gateway.parse_documents(request)).batch_id == batch_id
    with pytest.raises(GatewayConflict):
        await gateway.parse_documents(
            ParseDocumentsRequest(
                sources=[DocumentSource(url="https://files.example.test/b.pdf")],
                idempotency_key="parse-remote",
            )
        )


@pytest.mark.asyncio
async def test_completed_status_issues_and_decorates_digest_backed_recovery_token() -> None:
    file_id, batch_id = str(uuid4()), str(uuid4())
    artifact = SimpleNamespace(
        artifact_id="artifact-" + "b" * 64, file_id=file_id, result_version=2,
        available=True, expires_at=NOW + timedelta(hours=1),
    )
    batch = SimpleNamespace(
        id=batch_id, status=BatchStatus.COMPLETED, progress=100,
        total_files=1, completed_files=1, failed_files=0,
    )
    file = SimpleNamespace(
        id=file_id, status=FileStatus.COMPLETED, stage=ProcessingStage.COMPLETED,
        progress=100, last_error_code=None,
    )

    class Orientation:
        async def issue(self, binding, *, now):
            assert isinstance(binding, RecoveryTokenBinding)
            assert binding.suspected_pages == (1, 2)
            return SimpleNamespace(token="or_" + "a" * 64)

    gateway = ProductionDocumentGateway(
        intake=SimpleNamespace(),
        uploads=SimpleNamespace(get=lambda *_: _async_value(SimpleNamespace(page_count=2))),
        tasks=SimpleNamespace(
            get_batch=lambda *_: _async_value(batch),
            list_batch_files=lambda *_: _async_value((file,)),
        ),
        orchestration=SimpleNamespace(),
        artifacts=SimpleNamespace(list_for_batch=lambda *_: _async_value((artifact,))),
        recovery=SimpleNamespace(), orientation_issuer=Orientation(),
        now_factory=lambda: NOW,
    )
    status = await gateway.get_task_status(batch_id)
    assert status.files[0].recovery_token == "or_" + "a" * 64
