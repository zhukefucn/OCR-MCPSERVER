from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ocr_mcp_server.api.production_gateway import ProductionDocumentGateway
from ocr_mcp_server.domain.models import (
    BatchStatus,
    FileStatus,
    ProcessingStage,
)
from ocr_mcp_server.domain.orientation import OrientationAssessmentState
from ocr_mcp_server.infra.database import (
    create_database_engine,
    create_session_factory,
    initialize_schema,
)
from ocr_mcp_server.infra.orientation_assessment_repository import (
    OrientationAssessmentRepository,
)
from ocr_mcp_server.infra.task_repository import TaskRepository


NOW = datetime(2026, 7, 23, tzinfo=UTC)


def _database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest.mark.asyncio
async def test_assessment_is_claimed_once_and_survives_repository_restart(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(_database_url(tmp_path / "assessment.sqlite3"))
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    tasks = TaskRepository(sessions)
    file_id, batch_key = str(uuid4()), str(uuid4())
    created = await tasks.create_batch(batch_key, [file_id])
    first = OrientationAssessmentRepository(sessions)
    restarted = OrientationAssessmentRepository(create_session_factory(engine))
    try:
        claims = await asyncio.gather(
            *(
                first.begin(
                    file_id=file_id,
                    batch_id=created.batch.id,
                    result_version=2,
                    page_count=4,
                    now=NOW,
                )
                for _ in range(4)
            )
        )
        assert claims.count(True) == 1
        await first.complete(
            file_id=file_id,
            result_version=2,
            suspected_pages=(4, 2),
            now=NOW,
        )

        snapshot = await restarted.get(file_id, 2)
        assert snapshot is not None
        assert snapshot.state is OrientationAssessmentState.READY
        assert snapshot.suspected_pages == (2, 4)
        assert snapshot.evidence_ready is True
        assert (
            await restarted.begin(
                file_id=file_id,
                batch_id=created.batch.id,
                result_version=2,
                page_count=4,
                now=NOW,
            )
            is False
        )

        class Issuer:
            async def has_issued_source(self, *args):
                return False

            async def issue_once(self, binding, *, now):
                assert binding.suspected_pages == (2, 4)
                return SimpleNamespace(token="or_" + "f" * 64)

        batch = SimpleNamespace(
            id=created.batch.id,
            status=BatchStatus.COMPLETED,
            progress=100,
            total_files=1,
            completed_files=1,
            failed_files=0,
        )
        file = SimpleNamespace(
            id=file_id,
            status=FileStatus.COMPLETED,
            stage=ProcessingStage.COMPLETED,
            progress=100,
            last_error_code=None,
        )
        artifact = SimpleNamespace(
            artifact_id="artifact-" + "a" * 64,
            file_id=file_id,
            result_version=2,
            available=True,
            expires_at=NOW + timedelta(hours=1),
        )
        gateway = ProductionDocumentGateway(
            intake=SimpleNamespace(),
            uploads=SimpleNamespace(),
            tasks=SimpleNamespace(
                get_batch=lambda *_: _async_value(batch),
                list_batch_files=lambda *_: _async_value((file,)),
            ),
            orchestration=SimpleNamespace(),
            artifacts=SimpleNamespace(
                list_for_batch=lambda *_: _async_value((artifact,))
            ),
            recovery=SimpleNamespace(),
            orientation_issuer=Issuer(),
            orientation_assessments=restarted,
            now_factory=lambda: NOW,
        )
        status = await gateway.get_task_status(created.batch.id)
        assert status.files[0].recovery_token == "or_" + "f" * 64
    finally:
        await engine.dispose()


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_no_suspicion_and_failure_are_content_free_terminal_states(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(_database_url(tmp_path / "assessment.sqlite3"))
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    tasks = TaskRepository(sessions)
    repo = OrientationAssessmentRepository(sessions)
    first_id, second_id = str(uuid4()), str(uuid4())
    created = await tasks.create_batch(str(uuid4()), [first_id, second_id])
    try:
        assert await repo.begin(
            file_id=first_id,
            batch_id=created.batch.id,
            result_version=2,
            page_count=1,
            now=NOW,
        )
        await repo.complete(
            file_id=first_id,
            result_version=2,
            suspected_pages=(),
            now=NOW,
        )
        assert await repo.begin(
            file_id=second_id,
            batch_id=created.batch.id,
            result_version=2,
            page_count=1,
            now=NOW,
        )
        await repo.fail(
            file_id=second_id,
            result_version=2,
            error_code="orientation_detection_failed",
            now=NOW,
        )

        no_suspicion = await repo.get(first_id, 2)
        failed = await repo.get(second_id, 2)
        assert no_suspicion is not None
        assert no_suspicion.state is OrientationAssessmentState.NO_SUSPICION
        assert no_suspicion.suspected_pages == ()
        assert failed is not None
        assert failed.state is OrientationAssessmentState.FAILED
        assert failed.suspected_pages == ()
        assert "secret" not in repr((no_suspicion, failed))
    finally:
        await engine.dispose()
