from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

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
                    lease_seconds=10,
                )
                for _ in range(4)
            )
        )
        claimed = [claim for claim in claims if claim is not None]
        assert len(claimed) == 1
        await first.complete(
            claimed[0],
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
                lease_seconds=10,
            )
            is None
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
        first_claim = await repo.begin(
            file_id=first_id,
            batch_id=created.batch.id,
            result_version=2,
            page_count=1,
            now=NOW,
            lease_seconds=10,
        )
        assert first_claim is not None
        await repo.complete(
            first_claim,
            suspected_pages=(),
            now=NOW,
        )
        second_claim = await repo.begin(
            file_id=second_id,
            batch_id=created.batch.id,
            result_version=2,
            page_count=1,
            now=NOW,
            lease_seconds=10,
        )
        assert second_claim is not None
        await repo.fail(
            second_claim,
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


@pytest.mark.asyncio
async def test_stale_detection_is_taken_over_and_old_worker_cannot_write(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(_database_url(tmp_path / "takeover.sqlite3"))
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    tasks = TaskRepository(sessions)
    file_id = str(uuid4())
    created = await tasks.create_batch(str(uuid4()), [file_id])
    first_process = OrientationAssessmentRepository(sessions)
    restarted = OrientationAssessmentRepository(create_session_factory(engine))
    try:
        old_claim = await first_process.begin(
            file_id=file_id,
            batch_id=created.batch.id,
            result_version=2,
            page_count=2,
            now=NOW,
            lease_seconds=5,
        )
        assert old_claim is not None
        assert (
            await restarted.begin(
                file_id=file_id,
                batch_id=created.batch.id,
                result_version=2,
                page_count=2,
                now=NOW + timedelta(seconds=4),
                lease_seconds=5,
            )
            is None
        )
        new_claim = await restarted.begin(
            file_id=file_id,
            batch_id=created.batch.id,
            result_version=2,
            page_count=2,
            now=NOW + timedelta(seconds=5),
            lease_seconds=5,
        )
        assert new_claim is not None
        assert new_claim.claim_token != old_claim.claim_token
        with pytest.raises(Exception) as stale:
            await first_process.complete(
                old_claim,
                suspected_pages=(1,),
                now=NOW + timedelta(seconds=5),
            )
        assert getattr(stale.value, "code", None) == "orientation_request_conflict"
        completed = await restarted.complete(
            new_claim,
            suspected_pages=(2,),
            now=NOW + timedelta(seconds=6),
        )
        assert completed.suspected_pages == (2,)
        assert completed.attempt_count == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_assessment_schema_adds_claim_columns_and_null_claim_is_stale(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-assessment.sqlite3"
    engine = create_database_engine(_database_url(path))
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    tasks = TaskRepository(sessions)
    file_id = str(uuid4())
    created = await tasks.create_batch(str(uuid4()), [file_id])
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP TABLE orientation_assessments"))
            await connection.execute(
                text(
                    "CREATE TABLE orientation_assessments ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "file_id VARCHAR(512) NOT NULL,"
                    "batch_id VARCHAR(36) NOT NULL,"
                    "result_version INTEGER NOT NULL,"
                    "page_count INTEGER NOT NULL,"
                    "state VARCHAR(24) NOT NULL,"
                    "suspected_pages VARCHAR(2048) NOT NULL,"
                    "error_code VARCHAR(64),"
                    "created_at DATETIME NOT NULL,"
                    "updated_at DATETIME NOT NULL,"
                    "UNIQUE(file_id, result_version))"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO orientation_assessments "
                    "(file_id,batch_id,result_version,page_count,state,"
                    "suspected_pages,error_code,created_at,updated_at) "
                    "VALUES (:file_id,:batch_id,2,1,'detecting','',NULL,:now,:now)"
                ),
                {"file_id": file_id, "batch_id": created.batch.id, "now": NOW},
            )
        await initialize_schema(engine)
        async with engine.connect() as connection:
            columns = {
                row[1]
                for row in (
                    await connection.exec_driver_sql(
                        "PRAGMA table_info(orientation_assessments)"
                    )
                )
            }
        assert {"claim_token", "lease_expires_at", "attempt_count"} <= columns
        claim = await OrientationAssessmentRepository(sessions).begin(
            file_id=file_id,
            batch_id=created.batch.id,
            result_version=2,
            page_count=1,
            now=NOW + timedelta(seconds=1),
            lease_seconds=5,
        )
        assert claim is not None
        assert claim.attempt == 1
    finally:
        await engine.dispose()
