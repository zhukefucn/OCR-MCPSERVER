from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from ocr_mcp_server.domain.errors import InputValidationError, StateTransitionError
from ocr_mcp_server.domain.models import BatchStatus, FileStatus, ProcessingStage
from ocr_mcp_server.domain.progress import ProgressCounters, ProgressUnit
from ocr_mcp_server.domain.tasks import FileTaskSnapshot, StageEventSnapshot
from ocr_mcp_server.infra.database import (
    create_database_engine,
    create_session_factory,
    initialize_schema,
)
from ocr_mcp_server.infra.task_repository import TaskRepository


@pytest_asyncio.fixture
async def progress_repository(tmp_path: Path):
    engine = create_database_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'progress.sqlite3').as_posix()}",
        busy_timeout_ms=5000,
    )
    await initialize_schema(engine)
    repository = TaskRepository(create_session_factory(engine))
    try:
        yield repository
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_queued_initial_progress_and_batch_runtime_counts(progress_repository) -> None:
    repository = progress_repository
    created = await repository.create_batch("progress-batch", ["file-a", "file-b"])

    assert [item.progress for item in created.files] == [12, 12]
    assert created.batch.progress == 12
    assert created.batch.queued_files == 2
    assert created.batch.processing_files == 0
    assert created.batch.current_file_id is None
    assert all(item.counters is None for item in created.files)

    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = await repository.claim_next("worker-1", now=now, lease_seconds=30)
    second = await repository.claim_next("worker-2", now=now, lease_seconds=30)
    batch = await repository.get_batch(created.batch.id)

    assert first is not None and second is not None
    assert batch.processing_files == 2
    assert batch.queued_files == 0
    assert batch.current_file_id == "file-a"
    assert batch.progress == 12


@pytest.mark.asyncio
async def test_progress_updates_persist_exact_counters_and_never_regress(
    progress_repository,
) -> None:
    repository = progress_repository
    await repository.create_batch("updates", ["file-a"])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    claim = await repository.claim_next("worker", now=now, lease_seconds=60)
    assert claim is not None

    first = await repository.update_progress(
        claim.file.id,
        claim.lease_token,
        stage=ProcessingStage.MINERU_PARSING,
        counters=ProgressCounters(1, 4, ProgressUnit.PAGES),
        now=now + timedelta(seconds=1),
    )
    second = await repository.update_progress(
        claim.file.id,
        claim.lease_token,
        stage=ProcessingStage.MINERU_PARSING,
        counters=ProgressCounters(3, 4, ProgressUnit.PAGES),
        now=now + timedelta(seconds=2),
    )
    advanced = await repository.update_progress(
        claim.file.id,
        claim.lease_token,
        stage=ProcessingStage.COLLECTING_IMAGES,
        counters=ProgressCounters(0, None, ProgressUnit.IMAGES),
        now=now + timedelta(seconds=3),
    )

    assert (first.progress, second.progress, advanced.progress) == (24, 48, 60)
    assert second.counters == ProgressCounters(3, 4, ProgressUnit.PAGES)
    assert advanced.counters == ProgressCounters(0, None, ProgressUnit.IMAGES)
    with pytest.raises(StateTransitionError):
        await repository.update_progress(
            claim.file.id,
            claim.lease_token,
            stage=ProcessingStage.MINERU_PARSING,
            counters=ProgressCounters(4, 4, ProgressUnit.PAGES),
            now=now + timedelta(seconds=4),
        )
    with pytest.raises(StateTransitionError):
        await repository.update_progress(
            claim.file.id,
            claim.lease_token,
            stage=ProcessingStage.COLLECTING_IMAGES,
            counters=ProgressCounters(0, None, ProgressUnit.IMAGES),
            now=now + timedelta(seconds=4),
        )


@pytest.mark.asyncio
async def test_terminal_invariants_ordered_events_and_no_orm_leakage(
    progress_repository,
) -> None:
    repository = progress_repository
    created = await repository.create_batch("events", ["file-a"])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    claim = await repository.claim_next("worker", now=now, lease_seconds=60)
    assert claim is not None
    progressed = await repository.update_progress(
        claim.file.id,
        claim.lease_token,
        stage=ProcessingStage.MERGING,
        counters=ProgressCounters(1, 2, ProgressUnit.ITEMS),
        now=now + timedelta(seconds=1),
    )
    completed = await repository.complete_file(
        claim.file.id,
        claim.lease_token,
        with_warnings=True,
        now=now + timedelta(seconds=2),
    )

    files = await repository.list_batch_files(created.batch.id)
    events = await repository.list_batch_events(created.batch.id)
    batch = await repository.get_batch(created.batch.id)

    assert files == (completed,)
    assert all(isinstance(item, FileTaskSnapshot) for item in files)
    assert all(isinstance(item, StageEventSnapshot) for item in events)
    assert [item.version for item in events] == [2, progressed.version, completed.version]
    assert events[-1].new_status is FileStatus.COMPLETED_WITH_WARNINGS
    assert events[-1].new_progress == 100
    assert completed.stage is ProcessingStage.COMPLETED_WITH_WARNINGS
    assert completed.counters is None
    assert batch.status is BatchStatus.COMPLETED
    assert (batch.completed_files, batch.progress) == (1, 100)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        events[0].new_progress = 1  # type: ignore[misc]


@pytest.mark.asyncio
async def test_non_retryable_failure_is_atomic_and_does_not_consume_future_attempts(
    progress_repository,
) -> None:
    repository = progress_repository
    created = await repository.create_batch(
        "non-retryable", ["file-a"], max_attempts=5
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    claim = await repository.claim_next("worker", now=now, lease_seconds=60)
    assert claim is not None

    failed = await repository.fail_file(
        claim.file.id,
        claim.lease_token,
        error_code="document_invalid",
        now=now + timedelta(seconds=1),
    )

    assert failed.status is FileStatus.FAILED
    assert failed.stage is ProcessingStage.FAILED
    assert failed.progress == 100
    assert failed.attempt_count == 1
    assert failed.last_error_code == "document_invalid"
    assert (await repository.get_batch(created.batch.id)).status is BatchStatus.FAILED


@pytest.mark.asyncio
async def test_retry_attempt_restarts_stages_without_regressing_achieved_progress(
    progress_repository,
) -> None:
    repository = progress_repository
    await repository.create_batch("retry-resume", ["file-a"], max_attempts=2)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first_claim = await repository.claim_next("worker", now=now, lease_seconds=60)
    assert first_claim is not None
    merging = await repository.update_progress(
        first_claim.file.id,
        first_claim.lease_token,
        stage=ProcessingStage.MERGING,
        counters=ProgressCounters(1, 2, ProgressUnit.ITEMS),
        now=now + timedelta(seconds=1),
    )
    assert merging.progress == 91
    queued = await repository.retry_or_fail(
        first_claim.file.id,
        first_claim.lease_token,
        error_code="pipeline_dependency_unavailable",
        now=now + timedelta(seconds=2),
    )
    second_claim = await repository.claim_next(
        "worker", now=now + timedelta(seconds=3), lease_seconds=60
    )
    assert second_claim is not None

    restarted = await repository.update_progress(
        second_claim.file.id,
        second_claim.lease_token,
        stage=ProcessingStage.MINERU_PARSING,
        counters=ProgressCounters(1, 10, ProgressUnit.PAGES),
        now=now + timedelta(seconds=4),
    )
    advanced = await repository.update_progress(
        second_claim.file.id,
        second_claim.lease_token,
        stage=ProcessingStage.COLLECTING_IMAGES,
        counters=ProgressCounters(1, 2, ProgressUnit.IMAGES),
        now=now + timedelta(seconds=5),
    )

    assert queued.progress == second_claim.file.progress == 91
    assert restarted.stage is ProcessingStage.MINERU_PARSING
    assert restarted.progress == 91
    assert advanced.stage is ProcessingStage.COLLECTING_IMAGES
    assert advanced.progress == 91
    with pytest.raises(StateTransitionError):
        await repository.update_progress(
            second_claim.file.id,
            second_claim.lease_token,
            stage=ProcessingStage.MINERU_PARSING,
            counters=ProgressCounters(2, 10, ProgressUnit.PAGES),
            now=now + timedelta(seconds=6),
        )


@pytest.mark.asyncio
async def test_repository_rejects_boolean_progress_and_unsafe_enums_before_mutation(
    progress_repository,
) -> None:
    repository = progress_repository
    created = await repository.create_batch("invalid-values", ["file-a"])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    claim = await repository.claim_next("worker", now=now, lease_seconds=60)
    assert claim is not None

    with pytest.raises(InputValidationError):
        await repository.transition_file(
            claim.file.id,
            claim.lease_token,
            status=FileStatus.PROCESSING,
            stage=ProcessingStage.MINERU_PARSING,
            progress=True,  # type: ignore[arg-type]
            now=now + timedelta(seconds=1),
        )
    with pytest.raises(InputValidationError):
        await repository.transition_file(
            claim.file.id,
            claim.lease_token,
            status="customer-private-status",  # type: ignore[arg-type]
            stage=ProcessingStage.MINERU_PARSING,
            progress=20,
            now=now + timedelta(seconds=1),
        )

    persisted = await repository.get_file(created.files[0].id)
    assert persisted.version == claim.file.version
    assert persisted.progress == 12
