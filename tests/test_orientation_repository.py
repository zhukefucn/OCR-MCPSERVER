from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from ocr_mcp_server.domain.orientation import (
    OrientationErrorCode,
    OrientationFailure,
    RecoveryState,
    RecoveryTokenBinding,
)
from ocr_mcp_server.infra.database import (
    create_database_engine,
    create_session_factory,
    initialize_schema,
)
from ocr_mcp_server.infra.orientation_repository import OrientationRecoveryRepository
from ocr_mcp_server.infra.retention_repository import RetentionRepository
from ocr_mcp_server.infra.task_repository import TaskRepository
from ocr_mcp_server.infra.task_models import (
    ArtifactRecord,
    FileTaskRecord,
    RetentionRecord,
)
from ocr_mcp_server.services.orientation_recovery import (
    FullRecoveryPipelineSubmission,
    OrientationRecoveryCoordinator,
)
from ocr_mcp_server.services.observability import RecoveryOutcome
from ocr_mcp_server.domain.models import BatchStatus
from ocr_mcp_server.domain.errors import RetentionErrorCode, RetentionFailure
from ocr_mcp_server.services.retention import RetentionService


NOW = datetime(2026, 7, 22, tzinfo=UTC)
FILE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACCEPTED_FILE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
EXPECTED_CORRECTED_FILE_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


def takeover_proof() -> dict[str, object]:
    return {
        "adopted_source_file_id": EXPECTED_CORRECTED_FILE_ID,
        "accepted_input_file_id": ACCEPTED_FILE_ID,
        "accepted_input_sha256": "b" * 64,
        "accepted_input_size_bytes": 10,
    }


async def bind_expected(repo, claim, *, now=NOW):
    return await repo.bind_corrected_input(
        claim,
        corrected_input_version=3,
        corrected_file_id=EXPECTED_CORRECTED_FILE_ID,
        corrected_sha256="b" * 64,
        corrected_size_bytes=10,
        now=now,
    )


def database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest.mark.asyncio
async def test_schema_upgrade_adds_durable_takeover_proof_columns(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE orientation_recoveries (token_digest VARCHAR(64) PRIMARY KEY)"
        )
    engine = create_database_engine(database_url(path))
    try:
        await initialize_schema(engine)
        async with engine.connect() as connection:
            columns = {
                row[1]
                for row in (
                    await connection.exec_driver_sql(
                        "PRAGMA table_info(orientation_recoveries)"
                    )
                )
            }
        assert {
            "accepted_input_file_id",
            "accepted_input_sha256",
            "accepted_input_size_bytes",
            "corrected_input_file_id",
            "corrected_input_sha256",
            "corrected_input_size_bytes",
            "expected_corrected_input_version",
            "terminal_observed",
        }.issubset(columns)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_schema_upgrade_expands_recovery_file_limits_without_data_loss(
    repository,
) -> None:
    repo, engine, batch_id, _ = repository
    issue = await repo.issue(binding(batch_id), now=NOW)
    digest = sha256(issue.token.encode("utf-8")).hexdigest()
    path = Path(str(engine.url.database))
    await engine.dispose()

    with sqlite3.connect(path) as connection:
        schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'orientation_recoveries'"
        ).fetchone()[0]
        if "62914560" in schema:
            assert schema.count("62914560") == 2
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute(
                "UPDATE sqlite_master SET sql = replace(sql, '62914560', '31457280') "
                "WHERE type = 'table' AND name = 'orientation_recoveries'"
            )
            connection.execute("PRAGMA writable_schema=OFF")

    restarted = create_database_engine(database_url(path))
    try:
        await initialize_schema(restarted)
        await initialize_schema(restarted)
        async with restarted.connect() as connection:
            schema = (
                await connection.exec_driver_sql(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'orientation_recoveries'"
                )
            ).scalar_one()
            retained = await connection.scalar(
                text(
                    "SELECT count(*) FROM orientation_recoveries "
                    "WHERE token_digest = :digest"
                ),
                {"digest": digest},
            )
        assert schema.count("62914560") == 2
        assert "31457280" not in schema
        assert retained == 1
    finally:
        await restarted.dispose()


@pytest_asyncio.fixture
async def repository(tmp_path: Path):
    engine = create_database_engine(database_url(tmp_path / "recovery.sqlite3"))
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    tasks = TaskRepository(sessions)
    created = await tasks.create_batch("orientation-fixture", [FILE_ID])
    result = await tasks.create_batch(
        "orientation-result-fixture", ["bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"]
    )
    async with sessions() as session:
        retention = await session.get(RetentionRecord, created.batch.id)
        retention.content_due_at = NOW + timedelta(hours=20)
        session.add(
            ArtifactRecord(
                id="artifact-" + "a" * 64,
                file_id=FILE_ID,
                batch_id=created.batch.id,
                source_version=1,
                result_version=2,
                storage_key=f"{created.batch.id}/artifact.zip",
                media_type="application/zip",
                size_bytes=1,
                sha256="b" * 64,
                manifest_sha256="c" * 64,
                audit_metadata_sha256="d" * 64,
                audit_record_count=0,
                created_at=NOW,
                expires_at=NOW + timedelta(hours=20),
                available=True,
                deleted_at=None,
                version=1,
            )
        )
        await session.commit()
    repo = OrientationRecoveryRepository(sessions)
    try:
        yield repo, engine, created.batch.id, result.batch.id
    finally:
        await engine.dispose()


def binding(batch_id: str, **overrides) -> RecoveryTokenBinding:
    values = {
        "file_id": FILE_ID,
        "batch_id": batch_id,
        "source_result_version": 2,
        "page_count": 5,
        "suspected_pages": (2, 4),
        "expires_at": NOW + timedelta(hours=12),
    }
    values.update(overrides)
    return RecoveryTokenBinding(**values)


@pytest.mark.asyncio
async def test_issue_persists_only_digest_and_content_free_columns(repository) -> None:
    repo, engine, batch_id, _ = repository
    issue = await repo.issue(binding(batch_id), now=NOW)
    digest = sha256(issue.token.encode("utf-8")).hexdigest()

    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: {item["name"] for item in inspect(sync).get_columns("orientation_recoveries")}
        )
        unique_constraints = await connection.run_sync(
            lambda sync: inspect(sync).get_unique_constraints(
                "orientation_recoveries"
            )
        )
        row = (await connection.execute(text("SELECT * FROM orientation_recoveries"))).mappings().one()

    assert issue.snapshot.state is RecoveryState.ISSUED
    assert row["token_digest"] == digest
    assert issue.token not in repr(issue)
    assert issue.token not in " ".join(str(value) for value in row.values())
    assert not {"token", "text", "content", "filename", "url", "path"} & columns
    assert row["suspected_pages"] == "2,4"
    assert {"file_id", "source_result_version"} in {
        frozenset(item["column_names"]) for item in unique_constraints
    }


@pytest.mark.asyncio
async def test_issue_rejects_second_token_for_same_file_result_version(repository) -> None:
    repo, _, batch_id, _ = repository
    await repo.issue(binding(batch_id), now=NOW)
    with pytest.raises(OrientationFailure) as caught:
        await repo.issue(binding(batch_id), now=NOW)
    assert caught.value.code == OrientationErrorCode.REQUEST_CONFLICT.value


@pytest.mark.asyncio
async def test_issue_once_returns_raw_token_once_and_restart_keeps_it_usable(
    repository,
) -> None:
    repo, engine, batch_id, _ = repository
    first = await repo.issue_once(binding(batch_id), now=NOW)
    restarted = OrientationRecoveryRepository(create_session_factory(engine))

    assert first is not None
    assert await restarted.has_issued_source(FILE_ID, 2)
    assert await restarted.issue_once(binding(batch_id), now=NOW) is None
    claimed = await restarted.claim(first.token, (2,), now=NOW)
    assert claimed.snapshot.suspected_pages == (2, 4)


@pytest.mark.asyncio
async def test_concurrent_issue_once_has_exactly_one_raw_token(repository) -> None:
    repo, _, batch_id, _ = repository

    results = await asyncio.gather(
        *(repo.issue_once(binding(batch_id), now=NOW) for _ in range(4))
    )

    issued = [result for result in results if result is not None]
    assert len(issued) == 1


@pytest.mark.asyncio
async def test_list_claimed_is_bounded_and_keeps_claim_for_durable_reconciliation(repository) -> None:
    repo, engine, batch_id, _ = repository
    issue = await repo.issue(binding(batch_id), now=NOW)
    claimed = await repo.claim(issue.token, (4, 2), now=NOW)

    recovered = await repo.list_claimed(now=NOW, limit=1)

    assert len(recovered) == 1
    assert recovered[0] == replace(claimed, acquired=False)
    assert await repo.list_claimed(
        now=NOW + timedelta(hours=20), limit=1
    ) == recovered
    for invalid_limit in (0, 1001, True):
        with pytest.raises(OrientationFailure) as caught:
            await repo.list_claimed(now=NOW, limit=invalid_limit)
        assert caught.value.code == OrientationErrorCode.REQUEST_INVALID.value


@pytest.mark.asyncio
async def test_metadata_purge_cascades_orientation_rows(repository) -> None:
    repo, engine, batch_id, _ = repository
    issue = await repo.issue(binding(batch_id), now=NOW)
    await repo.claim(issue.token, (2,), now=NOW)
    sessions = create_session_factory(engine)
    retention = RetentionRepository(sessions)
    assert await retention.request_early_delete(batch_id, now=NOW)
    content_claim = await retention.claim_batch(
        batch_id, "cleanup", now=NOW, lease_seconds=60
    )
    await retention.complete_content(content_claim, now=NOW)
    metadata_claim = await retention.claim_batch(
        batch_id, "cleanup", now=NOW, lease_seconds=60
    )
    await retention.purge_metadata(metadata_claim, now=NOW)

    async with sessions() as session:
        remaining = await session.scalar(
            text("SELECT count(*) FROM orientation_recoveries WHERE batch_id=:batch_id"),
            {"batch_id": batch_id},
        )
    assert remaining == 0


@pytest.mark.asyncio
async def test_retention_claim_atomically_invalidates_claimed_recovery_and_retry_is_zero(
    repository,
) -> None:
    repo, engine, batch_id, _ = repository
    issue = await repo.issue(binding(batch_id), now=NOW)
    await repo.claim(issue.token, (2,), now=NOW)
    sessions = create_session_factory(engine)
    retention = RetentionRepository(sessions)
    assert await retention.request_early_delete(batch_id, now=NOW)
    claim = await retention.claim_batch(
        batch_id, "cleanup", now=NOW, lease_seconds=60
    )

    assert await retention.invalidate_orientation_recoveries(claim, now=NOW) == 1
    assert await retention.invalidate_orientation_recoveries(claim, now=NOW) == 0

    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT state, version FROM orientation_recoveries "
                    "WHERE batch_id=:batch_id"
                ),
                {"batch_id": batch_id},
            )
        ).mappings().one()
    assert row["state"] == RecoveryState.DELETED.value
    assert row["version"] == 3


@pytest.mark.asyncio
async def test_successful_token_invalidation_survives_physical_delete_failure(
    repository, tmp_path: Path
) -> None:
    repo, engine, batch_id, _ = repository
    issue = await repo.issue(binding(batch_id), now=NOW)
    sessions = create_session_factory(engine)
    retention = RetentionRepository(sessions)
    data_root = (tmp_path / "data").absolute()
    artifact_root = (tmp_path / "artifacts").absolute()
    (data_root / batch_id).mkdir(parents=True)
    (artifact_root / batch_id).mkdir(parents=True)

    class FailedDelete:
        def delete(self, *args, **kwargs):
            raise RetentionFailure(RetentionErrorCode.CLEANUP_FAILED)

    result = await RetentionService(
        retention,
        data_root,
        artifact_root,
        deleter=FailedDelete(),
    ).run_once(
        "cleanup",
        now=NOW + timedelta(hours=20),
        lease_seconds=60,
        limit=1,
    )

    assert (result.content_deleted, result.failed) == (0, 1)
    with pytest.raises(OrientationFailure) as deleted:
        await repo.resolve(issue.token, now=NOW)
    assert deleted.value.code == OrientationErrorCode.TOKEN_INVALID.value
    async with sessions() as session:
        state = await session.scalar(
            text(
                "SELECT state FROM orientation_recoveries "
                "WHERE batch_id=:batch_id"
            ),
            {"batch_id": batch_id},
        )
    assert state == RecoveryState.DELETED.value


@pytest.mark.asyncio
async def test_concurrent_restart_reconciliation_converges_without_running_work(repository) -> None:
    repo, _, batch_id, result_batch_id = repository
    issue = await repo.issue(binding(batch_id), now=NOW)
    claim = await repo.claim(issue.token, (2,), now=NOW)
    await bind_expected(repo, claim)

    class Runner:
        reconcile_calls = 0
        run_calls = 0

        async def reconcile(self, recovery_id):
            assert recovery_id == claim.claim_id
            self.reconcile_calls += 1
            await asyncio.sleep(0)
            return FullRecoveryPipelineSubmission(
                result_batch_id,
                BatchStatus.QUEUED,
                3,
                EXPECTED_CORRECTED_FILE_ID,
                ACCEPTED_FILE_ID,
                "b" * 64,
                10,
            )

        async def run(self, *args, **kwargs):
            self.run_calls += 1
            raise AssertionError("restart reconciliation cannot run work")

    runner = Runner()
    class Sink:
        def __init__(self):
            self.outcomes = []
        def observe_recovery(self, outcome):
            self.outcomes.append(outcome)
    sink = Sink()
    service = OrientationRecoveryCoordinator(
        repository=repo,
        detector=object(),
        corrector=object(),
        runner=runner,
        storage=object(),
        marker_registry=object(),
        content_write_guards=object(),
        now_factory=lambda: NOW,
        observability=sink,
    )

    first, second = await asyncio.gather(
        service.reconcile_incomplete(limit=1),
        service.reconcile_incomplete(limit=1),
    )

    assert first.scanned == second.scanned == 1
    assert first.completed + first.deferred == 1
    assert second.completed + second.deferred == 1
    assert runner.run_calls == 0
    assert sink.outcomes == [RecoveryOutcome.COMPLETED]
    resolved = await repo.resolve(issue.token, now=NOW)
    assert resolved.state is RecoveryState.COMPLETED
    assert (resolved.result_batch_id, resolved.result_version) == (result_batch_id, 3)


@pytest.mark.asyncio
async def test_concurrent_issue_creates_exactly_one_token_for_source_version(repository) -> None:
    repo, engine, batch_id, _ = repository
    outcomes = await asyncio.gather(
        *(repo.issue(binding(batch_id), now=NOW) for _ in range(8)),
        return_exceptions=True,
    )
    issued = [item for item in outcomes if not isinstance(item, BaseException)]
    failures = [item for item in outcomes if isinstance(item, OrientationFailure)]
    assert len(issued) == 1
    assert len(failures) == 7
    assert {item.code for item in failures} == {
        OrientationErrorCode.REQUEST_CONFLICT.value
    }
    async with engine.connect() as connection:
        count = await connection.scalar(text("SELECT count(*) FROM orientation_recoveries"))
    assert count == 1


@pytest.mark.asyncio
async def test_issue_enforces_content_retention_maximum(repository) -> None:
    repo, _, batch_id, _ = repository
    with pytest.raises(OrientationFailure) as exc_info:
        await repo.issue(
            binding(batch_id, expires_at=NOW + timedelta(hours=24, seconds=1)),
            now=NOW,
        )
    assert exc_info.value.code == OrientationErrorCode.TOKEN_INVALID.value


@pytest.mark.asyncio
async def test_resolve_rejects_unknown_expired_and_deleted_without_disclosure(repository) -> None:
    repo, engine, batch_id, _ = repository
    issue = await repo.issue(
        binding(batch_id, expires_at=NOW + timedelta(seconds=10)), now=NOW
    )

    resolved = await repo.resolve(issue.token, now=NOW)
    assert resolved.file_id == FILE_ID
    for token, when in (("unknown-secret", NOW), (issue.token, NOW + timedelta(seconds=10))):
        with pytest.raises(OrientationFailure) as exc_info:
            await repo.resolve(token, now=when)
        assert exc_info.value.code == OrientationErrorCode.TOKEN_INVALID.value
        assert token not in str(exc_info.value)

    await RetentionRepository(create_session_factory(engine)).request_early_delete(
        batch_id, now=NOW
    )
    await repo.invalidate_batch(batch_id, now=NOW)
    with pytest.raises(OrientationFailure) as deleted:
        await repo.resolve(issue.token, now=NOW)
    assert deleted.value.code == OrientationErrorCode.TOKEN_INVALID.value


@pytest.mark.asyncio
async def test_claim_is_atomic_idempotent_and_rejects_conflicting_page_sets(repository) -> None:
    repo, _, batch_id, _ = repository
    issue = await repo.issue(binding(batch_id), now=NOW)

    claims = await asyncio.gather(
        *(repo.claim(issue.token, (4, 2), now=NOW) for _ in range(6))
    )
    assert sum(claim.acquired for claim in claims) == 1
    assert len({claim.claim_id for claim in claims}) == 1
    assert {claim.snapshot.selected_pages for claim in claims} == {(2, 4)}
    assert len({claim.request_fingerprint for claim in claims}) == 1

    with pytest.raises(OrientationFailure) as exc_info:
        await repo.claim(issue.token, (2,), now=NOW)
    assert exc_info.value.code == OrientationErrorCode.REQUEST_CONFLICT.value


@pytest.mark.asyncio
async def test_claim_validates_requested_pages_against_binding(repository) -> None:
    repo, _, batch_id, _ = repository
    issue = await repo.issue(binding(batch_id), now=NOW)

    for pages in ((), (0,), (6,), (1,), (2, 2)):
        with pytest.raises(OrientationFailure) as exc_info:
            await repo.claim(issue.token, pages, now=NOW)
        assert exc_info.value.code == OrientationErrorCode.REQUEST_INVALID.value


@pytest.mark.asyncio
async def test_terminal_compare_and_set_is_idempotent_and_restart_safe(repository) -> None:
    repo, engine, batch_id, result_batch_id = repository
    issue = await repo.issue(binding(batch_id), now=NOW)
    claim = await repo.claim(issue.token, (2,), now=NOW)
    await bind_expected(repo, claim)

    restarted = OrientationRecoveryRepository(create_session_factory(engine))
    completed = await restarted.complete(
        claim,
        corrected_input_version=3,
        result_batch_id=result_batch_id,
        result_version=3,
        **takeover_proof(),
        now=NOW + timedelta(seconds=1),
    )
    repeated = await repo.complete(
        claim,
        corrected_input_version=3,
        result_batch_id=result_batch_id,
        result_version=3,
        **takeover_proof(),
        now=NOW + timedelta(seconds=2),
    )
    assert completed == repeated
    assert repeated.state is RecoveryState.COMPLETED
    async with engine.connect() as connection:
        proof = (
            await connection.execute(
                text(
                    "SELECT accepted_input_file_id, accepted_input_sha256, "
                    "accepted_input_size_bytes, corrected_input_file_id, "
                    "corrected_input_sha256, corrected_input_size_bytes, "
                    "expected_corrected_input_version FROM orientation_recoveries"
                )
            )
        ).mappings().one()
    assert proof == {
        "accepted_input_file_id": ACCEPTED_FILE_ID,
        "accepted_input_sha256": "b" * 64,
        "accepted_input_size_bytes": 10,
        "corrected_input_file_id": EXPECTED_CORRECTED_FILE_ID,
        "corrected_input_sha256": "b" * 64,
        "corrected_input_size_bytes": 10,
        "expected_corrected_input_version": 3,
    }

    with pytest.raises(OrientationFailure) as conflict:
        await repo.complete(
            claim,
            corrected_input_version=4,
            result_batch_id="33333333-3333-4333-8333-333333333333",
            result_version=4,
            **takeover_proof(),
            now=NOW + timedelta(seconds=3),
        )
    assert conflict.value.code == OrientationErrorCode.CLAIM_CONFLICT.value


@pytest.mark.asyncio
async def test_failure_transition_is_safe_compare_and_set(repository) -> None:
    repo, engine, batch_id, _ = repository
    issue = await repo.issue(binding(batch_id), now=NOW)
    claim = await repo.claim(issue.token, (4,), now=NOW)
    failed = await repo.fail(
        claim, state=RecoveryState.UNCERTAIN,
        error_code="orientation_uncertain", now=NOW,
    )
    repeated = await repo.fail(
        claim, state=RecoveryState.UNCERTAIN,
        error_code="orientation_uncertain", now=NOW + timedelta(seconds=1),
    )
    assert failed == repeated
    assert failed.error_code == "orientation_uncertain"

    async with engine.connect() as connection:
        row = (await connection.execute(text("SELECT * FROM orientation_recoveries"))).mappings().one()
    planted = "private bank statement text"
    assert planted not in repr(failed)
    assert planted not in " ".join(str(value) for value in row.values())


@pytest.mark.asyncio
async def test_terminal_transitions_reject_expired_deleted_or_forged_claims(repository) -> None:
    repo, engine, batch_id, result_batch_id = repository
    issue = await repo.issue(
        binding(batch_id, expires_at=NOW + timedelta(seconds=2)), now=NOW
    )
    claim = await repo.claim(issue.token, (2,), now=NOW)
    await bind_expected(repo, claim)

    with pytest.raises(OrientationFailure) as expired:
        await repo.fail(
            claim,
            state=RecoveryState.FAILED,
            error_code="orientation_failed",
            now=NOW + timedelta(seconds=2),
        )
    assert expired.value.code == OrientationErrorCode.CLAIM_CONFLICT.value

    forged = replace(
        claim,
        snapshot=replace(claim.snapshot, source_result_version=1),
    )
    with pytest.raises(OrientationFailure) as mismatch:
        await repo.complete(
            forged,
            corrected_input_version=3,
            result_batch_id=result_batch_id,
            result_version=3,
            **takeover_proof(),
            now=NOW,
        )
    assert mismatch.value.code == OrientationErrorCode.CLAIM_CONFLICT.value

    await RetentionRepository(create_session_factory(engine)).request_early_delete(
        batch_id, now=NOW
    )
    await repo.invalidate_batch(batch_id, now=NOW)
    with pytest.raises(OrientationFailure) as deleted:
        await repo.fail(
            claim,
            state=RecoveryState.FAILED,
            error_code="orientation_failed",
            now=NOW,
        )
    assert deleted.value.code == OrientationErrorCode.CLAIM_CONFLICT.value


@pytest.mark.asyncio
async def test_generated_token_always_matches_public_contract(monkeypatch, repository) -> None:
    repo, _, batch_id, _ = repository
    monkeypatch.setattr(
        "ocr_mcp_server.infra.orientation_repository.secrets.token_hex",
        lambda _: "a" * 64,
    )
    issue = await repo.issue(binding(batch_id), now=NOW)

    from ocr_mcp_server.api.contracts import OrientationReparseRequest

    assert issue.token == "or_" + "a" * 64
    assert OrientationReparseRequest(recovery_token=issue.token).recovery_token == issue.token


@pytest.mark.asyncio
async def test_retention_authoritatively_bounds_issue_and_all_later_operations(repository) -> None:
    repo, engine, batch_id, result_batch_id = repository
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE retention SET content_due_at = :due WHERE batch_id = :batch"),
            {"due": NOW + timedelta(hours=1), "batch": batch_id},
        )
    with pytest.raises(OrientationFailure) as beyond_due:
        await repo.issue(
            binding(batch_id, expires_at=NOW + timedelta(hours=1, seconds=1)), now=NOW
        )
    assert beyond_due.value.code == OrientationErrorCode.TOKEN_INVALID.value

    issue = await repo.issue(
        binding(batch_id, expires_at=NOW + timedelta(hours=1)), now=NOW
    )
    claim = await repo.claim(issue.token, (2,), now=NOW)
    await bind_expected(repo, claim)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE retention SET early_delete = 1, content_due_at = :now "
                "WHERE batch_id = :batch"
            ),
            {"now": NOW, "batch": batch_id},
        )

    for operation in (
        lambda: repo.resolve(issue.token, now=NOW),
        lambda: repo.claim(issue.token, (2,), now=NOW),
    ):
        with pytest.raises(OrientationFailure):
            await operation()

    completed = await repo.complete(
        claim,
        corrected_input_version=3,
        result_batch_id=result_batch_id,
        result_version=3,
        **takeover_proof(),
        now=NOW,
    )
    assert completed.state is RecoveryState.COMPLETED
    with pytest.raises(OrientationFailure):
        await repo.fail(
            claim,
            state=RecoveryState.FAILED,
            error_code="orientation_failed",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_durable_takeover_completes_after_token_and_content_expiry(repository) -> None:
    repo, _, batch_id, result_batch_id = repository
    issue = await repo.issue(
        binding(batch_id, expires_at=NOW + timedelta(seconds=1)), now=NOW
    )
    claim = await repo.claim(issue.token, (2,), now=NOW)
    await bind_expected(repo, claim)

    completed = await repo.complete(
        claim,
        corrected_input_version=3,
        result_batch_id=result_batch_id,
        result_version=3,
        **takeover_proof(),
        now=NOW + timedelta(hours=20),
    )

    assert completed.state is RecoveryState.COMPLETED


@pytest.mark.asyncio
async def test_restart_rejects_other_filetask_and_forged_digest_without_expected_binding(
    repository,
) -> None:
    repo, engine, batch_id, result_batch_id = repository
    issue = await repo.issue(binding(batch_id), now=NOW)
    claim = await repo.claim(issue.token, (2,), now=NOW)
    expected_file_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    await repo.bind_corrected_input(
        claim,
        corrected_input_version=3,
        corrected_file_id=expected_file_id,
        corrected_sha256="b" * 64,
        corrected_size_bytes=10,
        now=NOW,
    )
    other_file_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    sessions = create_session_factory(engine)
    async with sessions() as session:
        session.add(
            FileTaskRecord(
                id=other_file_id,
                batch_id=result_batch_id,
                position=1,
                status="queued",
                stage="validating",
                progress=0,
                attempt_count=0,
                max_attempts=3,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                last_error_code=None,
                completed_units=None,
                total_units=None,
                progress_unit=None,
                created_at=NOW,
                updated_at=NOW,
                version=1,
            )
        )
        await session.commit()

    with pytest.raises(OrientationFailure) as forged:
        await repo.complete(
            claim,
            corrected_input_version=3,
            result_batch_id=result_batch_id,
            result_version=3,
            adopted_source_file_id=expected_file_id,
            accepted_input_file_id=other_file_id,
            accepted_input_sha256="c" * 64,
            accepted_input_size_bytes=11,
            now=NOW,
        )
    assert forged.value.code == OrientationErrorCode.CLAIM_CONFLICT.value


@pytest.mark.asyncio
async def test_deleted_source_claim_reconciles_durable_runner_takeover(repository) -> None:
    repo, engine, batch_id, result_batch_id = repository
    issue = await repo.issue(binding(batch_id), now=NOW)
    claim = await repo.claim(issue.token, (2,), now=NOW)
    await bind_expected(repo, claim)
    retention = RetentionRepository(create_session_factory(engine))
    assert await retention.request_early_delete(batch_id, now=NOW)
    cleanup_claim = await retention.claim_batch(
        batch_id, "cleanup", now=NOW, lease_seconds=60
    )
    assert await retention.invalidate_orientation_recoveries(
        cleanup_claim, now=NOW
    ) == 1

    class Runner:
        async def reconcile(self, recovery_id):
            return FullRecoveryPipelineSubmission(
                result_batch_id,
                BatchStatus.QUEUED,
                3,
                EXPECTED_CORRECTED_FILE_ID,
                ACCEPTED_FILE_ID,
                "b" * 64,
                10,
            )

        async def run(self, *args, **kwargs):
            raise AssertionError("reconciliation must not run work")

    service = OrientationRecoveryCoordinator(
        repository=repo,
        detector=object(),
        corrector=object(),
        runner=Runner(),
        storage=object(),
        marker_registry=object(),
        content_write_guards=object(),
        now_factory=lambda: NOW + timedelta(days=1),
    )
    result = await service.reconcile_incomplete(limit=1)

    assert (result.completed, result.failed, result.deferred) == (1, 0, 0)
    async with engine.connect() as connection:
        state = await connection.scalar(
            text(
                "SELECT state FROM orientation_recoveries "
                "WHERE batch_id=:batch_id"
            ),
            {"batch_id": batch_id},
        )
    assert state == RecoveryState.COMPLETED.value
    with pytest.raises(OrientationFailure):
        await repo.resolve(issue.token, now=NOW + timedelta(days=1))


@pytest.mark.asyncio
async def test_invalidate_prevents_later_issue_and_tombstone_blocks_resolution(repository) -> None:
    repo, engine, batch_id, _ = repository
    issue = await repo.issue(binding(batch_id), now=NOW)
    retention_repo = RetentionRepository(create_session_factory(engine))

    with pytest.raises(OrientationFailure):
        await repo.invalidate_batch(batch_id, now=NOW)
    assert (await repo.resolve(issue.token, now=NOW)).state is RecoveryState.ISSUED
    assert await retention_repo.claim_due(
        "retention-worker", now=NOW, lease_seconds=30, limit=1
    ) == ()

    await retention_repo.request_early_delete(batch_id, now=NOW)
    await repo.invalidate_batch(batch_id, now=NOW)
    with pytest.raises(OrientationFailure):
        await repo.issue(binding(batch_id), now=NOW)

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE retention SET early_delete = 0, content_deleted_at = NULL, "
                "content_due_at = :due, data_tombstone = NULL WHERE batch_id = :batch"
            ),
            {"due": NOW + timedelta(hours=1), "batch": batch_id},
        )
    with pytest.raises(OrientationFailure) as duplicate:
        await repo.issue(
            binding(batch_id, expires_at=NOW + timedelta(hours=1)), now=NOW
        )
    assert duplicate.value.code == OrientationErrorCode.REQUEST_CONFLICT.value
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE retention SET data_tombstone = 'data-deleting' WHERE batch_id = :batch"),
            {"batch": batch_id},
        )
    with pytest.raises(OrientationFailure):
        await repo.resolve(issue.token, now=NOW)


@pytest.mark.asyncio
async def test_cancellation_propagates_instead_of_becoming_persistence_failure(
    monkeypatch, repository
) -> None:
    repo, _, batch_id, _ = repository
    issue = await repo.issue(binding(batch_id), now=NOW)

    def cancel(_record):
        raise asyncio.CancelledError()

    monkeypatch.setattr(repo, "_snapshot", cancel)
    with pytest.raises(asyncio.CancelledError):
        await repo.resolve(issue.token, now=NOW)


@pytest.mark.asyncio
async def test_content_deleted_at_blocks_new_token_issue(repository) -> None:
    repo, engine, batch_id, _ = repository
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE retention SET content_deleted_at = :now WHERE batch_id = :batch"),
            {"now": NOW, "batch": batch_id},
        )
    with pytest.raises(OrientationFailure) as deleted:
        await repo.issue(binding(batch_id), now=NOW)
    assert deleted.value.code == OrientationErrorCode.TOKEN_INVALID.value


@pytest.mark.asyncio
async def test_schema_checks_state_shape_and_corrupt_hydration_is_safely_mapped(repository) -> None:
    repo, engine, batch_id, _ = repository
    issue = await repo.issue(binding(batch_id), now=NOW)
    digest = sha256(issue.token.encode()).hexdigest()
    async with engine.connect() as connection:
        checks = await connection.run_sync(
            lambda sync: inspect(sync).get_check_constraints("orientation_recoveries")
        )
    check_sql = " ".join(str(item["sqltext"]) for item in checks).lower()
    assert "state in" in check_sql
    assert "state = 'issued'" in check_sql
    assert "state = 'completed'" in check_sql

    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        await connection.execute(
            text("UPDATE orientation_recoveries SET state = :planted WHERE token_digest = :digest"),
            {"planted": "client.pdf private text", "digest": digest},
        )
        await connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    with pytest.raises(OrientationFailure) as corrupted:
        await repo.resolve(issue.token, now=NOW)
    assert corrupted.value.code == OrientationErrorCode.PERSISTENCE_FAILED.value
    assert "client.pdf" not in repr(corrupted.value)


@pytest.mark.asyncio
async def test_corrupt_claim_hydration_also_maps_to_content_free_persistence_failure(repository) -> None:
    repo, engine, batch_id, _ = repository
    issue = await repo.issue(binding(batch_id), now=NOW)
    digest = sha256(issue.token.encode()).hexdigest()
    planted = "client.pdf private text"
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        await connection.execute(
            text("UPDATE orientation_recoveries SET state = :planted WHERE token_digest = :digest"),
            {"planted": planted, "digest": digest},
        )
        await connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    with pytest.raises(OrientationFailure) as corrupted:
        await repo.claim(issue.token, (2,), now=NOW)
    assert corrupted.value.code == OrientationErrorCode.PERSISTENCE_FAILED.value
    assert planted not in repr(corrupted.value)


@pytest.mark.asyncio
async def test_issue_requires_available_source_artifact_and_complete_requires_result_batch(repository) -> None:
    repo, engine, batch_id, _ = repository
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE artifacts SET available = 0 WHERE batch_id = :batch"),
            {"batch": batch_id},
        )
    with pytest.raises(OrientationFailure) as unavailable:
        await repo.issue(binding(batch_id), now=NOW)
    assert unavailable.value.code == OrientationErrorCode.TOKEN_INVALID.value

    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE artifacts SET available = 1 WHERE batch_id = :batch"),
            {"batch": batch_id},
        )
    issue = await repo.issue(binding(batch_id), now=NOW)
    claim = await repo.claim(issue.token, (2,), now=NOW)
    await bind_expected(repo, claim)
    with pytest.raises(OrientationFailure) as missing_result:
        await repo.complete(
            claim,
            corrected_input_version=3,
            result_batch_id="22222222-2222-4222-8222-222222222222",
            result_version=3,
            **takeover_proof(),
            now=NOW,
        )
    assert missing_result.value.code == OrientationErrorCode.CLAIM_CONFLICT.value
