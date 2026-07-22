from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text

from ocr_mcp_server.domain.errors import (
    InputValidationError,
    LeaseConflictError,
    StateTransitionError,
)
from ocr_mcp_server.domain.models import BatchStatus, FileStatus, ProcessingStage
from ocr_mcp_server.infra.database import (
    create_database_engine,
    create_session_factory,
    initialize_schema,
)
from ocr_mcp_server.infra.task_repository import TaskRepository


def database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest_asyncio.fixture
async def repository(tmp_path: Path):
    engine = create_database_engine(
        database_url(tmp_path / "tasks.sqlite3"), busy_timeout_ms=1777
    )
    await initialize_schema(engine)
    repo = TaskRepository(create_session_factory(engine))
    try:
        yield repo, engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_schema_and_sqlite_pragmas_are_explicit(repository) -> None:
    _, engine = repository
    async with engine.connect() as connection:
        foreign_keys = (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar()
        journal_mode = (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar()
        busy_timeout = (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar()
        table_columns = await connection.run_sync(
            lambda sync_connection: {
                table: {column["name"] for column in inspect(sync_connection).get_columns(table)}
                for table in ("batches", "file_tasks", "stage_events")
            }
        )

    assert foreign_keys == 1
    assert journal_mode.lower() == "wal"
    assert busy_timeout == 1777
    assert table_columns["stage_events"] == {
        "id", "file_id", "old_status", "new_status", "old_stage", "new_stage",
        "old_progress", "new_progress", "error_code", "created_at",
    }
    forbidden = {"message", "text", "content", "filename", "original_filename"}
    assert not any(forbidden & columns for columns in table_columns.values())


@pytest.mark.asyncio
async def test_create_batch_is_idempotent_and_validates_safe_identifiers(repository) -> None:
    repo, _ = repository
    first = await repo.create_batch("digest-1", ["file-a", "file-b"])
    repeated = await repo.create_batch("digest-1", ["ignored-file"])

    assert first.created is True
    assert repeated.created is False
    assert repeated.batch.id == first.batch.id
    assert [file.id for file in repeated.files] == ["file-a", "file-b"]
    assert [file.position for file in repeated.files] == [0, 1]
    assert all(value.created_at.tzinfo is UTC for value in (first.batch, *first.files))

    for key, files in (("", ["a"]), ("x", []), ("x", ["a", "a"])):
        with pytest.raises(InputValidationError):
            await repo.create_batch(key, files)


@pytest.mark.asyncio
async def test_concurrent_create_batch_converges_on_one_batch(repository) -> None:
    repo, _ = repository
    results = await asyncio.gather(
        *(repo.create_batch("same-digest", ["same-file"]) for _ in range(4))
    )

    assert len({result.batch.id for result in results}) == 1
    assert sum(result.created for result in results) == 1


@pytest.mark.asyncio
async def test_claim_order_is_stable_and_active_task_is_not_reclaimed(repository) -> None:
    repo, engine = repository
    first_batch = await repo.create_batch("batch-one", ["a", "b"])
    second_batch = await repo.create_batch("batch-two", ["c", "d"])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE batches SET created_at = :created_at"),
            {"created_at": now},
        )
    expected_ids = [
        file.id
        for result in sorted(
            (first_batch, second_batch), key=lambda result: result.batch.id
        )
        for file in result.files
    ]

    first = await repo.claim_next("worker-1", now=now, lease_seconds=30)
    second = await repo.claim_next("worker-2", now=now, lease_seconds=30)
    third = await repo.claim_next("worker-3", now=now, lease_seconds=30)
    fourth = await repo.claim_next("worker-4", now=now, lease_seconds=30)
    empty = await repo.claim_next("worker-5", now=now, lease_seconds=30)

    claims = [first, second, third, fourth]
    assert [claim.file.id for claim in claims] == expected_ids
    assert len({claim.lease_token for claim in claims}) == 4
    assert first.file.status is FileStatus.PROCESSING
    assert first.file.attempt_count == 1
    assert first.file.lease_owner == "worker-1"
    assert first.file.lease_token == first.lease_token
    assert first.file.lease_expires_at is not None
    assert first.expires_at == now + timedelta(seconds=30)
    assert empty is None


@pytest.mark.asyncio
async def test_transition_cannot_bypass_claim_to_create_unleased_processing_task(
    repository,
) -> None:
    repo, engine = repository
    created = await repo.create_batch("claim-only", ["claim-only-file"])
    now = datetime(2026, 1, 1, tzinfo=UTC)

    with pytest.raises(StateTransitionError) as exc_info:
        await repo.transition_file(
            created.files[0].id,
            "not-a-lease",
            status=FileStatus.PROCESSING,
            stage=ProcessingStage.QUEUED,
            progress=0,
            now=now,
        )

    persisted = await repo.get_file(created.files[0].id)
    async with engine.connect() as connection:
        event_count = (
            await connection.execute(text("SELECT count(*) FROM stage_events"))
        ).scalar_one()
    assert exc_info.value.code == "state_transition_invalid"
    assert persisted.status is FileStatus.QUEUED
    assert persisted.lease_owner is None
    assert persisted.lease_token is None
    assert persisted.lease_expires_at is None
    assert event_count == 0


@pytest.mark.asyncio
async def test_heartbeat_extends_valid_lease_and_rejects_bad_or_expired(repository) -> None:
    repo, _ = repository
    await repo.create_batch("heartbeat", ["heartbeat-file"])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    claim = await repo.claim_next("worker", now=now, lease_seconds=10)

    renewed = await repo.heartbeat(
        claim.file.id, claim.lease_token, now=now + timedelta(seconds=2), lease_seconds=20
    )
    assert renewed.lease_expires_at == now + timedelta(seconds=22)
    with pytest.raises(LeaseConflictError):
        await repo.heartbeat(claim.file.id, "wrong", now=now, lease_seconds=5)
    with pytest.raises(LeaseConflictError):
        await repo.heartbeat(
            claim.file.id,
            claim.lease_token,
            now=now + timedelta(seconds=23),
            lease_seconds=5,
        )


@pytest.mark.asyncio
async def test_transitions_write_safe_events_and_refresh_batch(repository) -> None:
    repo, engine = repository
    created = await repo.create_batch("transitions", ["good", "bad"])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    good = await repo.claim_next("worker", now=now, lease_seconds=60)
    progressed = await repo.transition_file(
        good.file.id,
        good.lease_token,
        status=FileStatus.PROCESSING,
        stage=ProcessingStage.MERGING,
        progress=75,
        now=now + timedelta(seconds=1),
    )
    completed = await repo.transition_file(
        progressed.id,
        good.lease_token,
        status=FileStatus.COMPLETED,
        stage=ProcessingStage.COMPLETED,
        progress=100,
        now=now + timedelta(seconds=2),
    )
    bad = await repo.claim_next("worker", now=now, lease_seconds=60)
    failed = await repo.transition_file(
        bad.file.id,
        bad.lease_token,
        status=FileStatus.FAILED,
        stage=ProcessingStage.FAILED,
        progress=100,
        error_code="ocr_failed",
        now=now + timedelta(seconds=2),
    )

    batch = await repo.get_batch(created.batch.id)
    assert completed.lease_token is None
    assert failed.last_error_code == "ocr_failed"
    assert batch.status is BatchStatus.COMPLETED_WITH_ERRORS
    assert (batch.completed_files, batch.failed_files, batch.progress) == (1, 1, 100)
    async with engine.connect() as connection:
        events = (await connection.execute(text("SELECT error_code FROM stage_events ORDER BY id"))).all()
    assert events == [(None,), (None,), ("ocr_failed",)]
    with pytest.raises(StateTransitionError):
        await repo.transition_file(
            completed.id, good.lease_token, status=FileStatus.FAILED,
            stage=ProcessingStage.FAILED, progress=100, now=now + timedelta(seconds=3)
        )


@pytest.mark.asyncio
async def test_retry_requeues_until_attempt_limit_then_fails(repository) -> None:
    repo, _ = repository
    created = await repo.create_batch("retry", ["retry-file"], max_attempts=2)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = await repo.claim_next("worker", now=now, lease_seconds=30)
    queued = await repo.retry_or_fail(
        first.file.id, first.lease_token, error_code="temporary", now=now
    )
    second = await repo.claim_next("worker", now=now, lease_seconds=30)
    failed = await repo.retry_or_fail(
        second.file.id, second.lease_token, error_code="permanent", now=now
    )

    assert queued.status is FileStatus.QUEUED
    assert queued.stage is ProcessingStage.QUEUED
    assert queued.lease_token is None
    assert failed.status is FileStatus.FAILED
    assert failed.progress == 100
    assert (await repo.get_batch(created.batch.id)).status is BatchStatus.FAILED


@pytest.mark.asyncio
async def test_restart_recovers_only_expired_processing_tasks(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite3"
    url = database_url(path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    engine = create_database_engine(url, busy_timeout_ms=5000)
    await initialize_schema(engine)
    repo = TaskRepository(create_session_factory(engine))
    await repo.create_batch("restart", ["expired", "complete"])
    expired = await repo.claim_next("worker", now=now, lease_seconds=5)
    complete = await repo.claim_next("worker", now=now, lease_seconds=60)
    await repo.transition_file(
        complete.file.id, complete.lease_token, status=FileStatus.COMPLETED,
        stage=ProcessingStage.COMPLETED, progress=100, now=now
    )
    await engine.dispose()

    restarted_engine = create_database_engine(url, busy_timeout_ms=5000)
    await initialize_schema(restarted_engine)
    restarted = TaskRepository(create_session_factory(restarted_engine))
    try:
        assert await restarted.recover_expired_leases(now=now + timedelta(seconds=6)) == 1
        recovered = await restarted.get_file(expired.file.id)
        untouched = await restarted.get_file(complete.file.id)
        assert recovered.status is FileStatus.QUEUED
        assert recovered.lease_token is None
        assert untouched.status is FileStatus.COMPLETED
        claim = await restarted.claim_next(
            "new-worker", now=now + timedelta(seconds=6), lease_seconds=30
        )
        assert claim.file.id == expired.file.id
        assert await restarted.claim_next(
            "new-worker", now=now + timedelta(seconds=6), lease_seconds=30
        ) is None
    finally:
        await restarted_engine.dispose()
