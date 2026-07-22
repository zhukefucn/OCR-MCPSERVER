from __future__ import annotations

import asyncio
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
from ocr_mcp_server.infra.task_models import ArtifactRecord, RetentionRecord


NOW = datetime(2026, 7, 22, tzinfo=UTC)
FILE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


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
        row = (await connection.execute(text("SELECT * FROM orientation_recoveries"))).mappings().one()

    assert issue.snapshot.state is RecoveryState.ISSUED
    assert row["token_digest"] == digest
    assert issue.token not in repr(issue)
    assert issue.token not in " ".join(str(value) for value in row.values())
    assert not {"token", "text", "content", "filename", "url", "path"} & columns
    assert row["suspected_pages"] == "2,4"


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

    restarted = OrientationRecoveryRepository(create_session_factory(engine))
    completed = await restarted.complete(
        claim,
        corrected_input_version=3,
        result_batch_id=result_batch_id,
        result_version=3,
        now=NOW + timedelta(seconds=1),
    )
    repeated = await repo.complete(
        claim,
        corrected_input_version=3,
        result_batch_id=result_batch_id,
        result_version=3,
        now=NOW + timedelta(seconds=2),
    )
    assert completed == repeated
    assert repeated.state is RecoveryState.COMPLETED

    with pytest.raises(OrientationFailure) as conflict:
        await repo.complete(
            claim,
            corrected_input_version=4,
            result_batch_id="33333333-3333-4333-8333-333333333333",
            result_version=4,
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

    with pytest.raises(OrientationFailure) as expired:
        await repo.fail(
            claim,
            state=RecoveryState.FAILED,
            error_code="orientation_failed",
            now=NOW + timedelta(seconds=2),
        )
    assert expired.value.code == OrientationErrorCode.CLAIM_CONFLICT.value

    fresh = await repo.issue(binding(batch_id), now=NOW)
    fresh_claim = await repo.claim(fresh.token, (4,), now=NOW)
    forged = replace(
        fresh_claim,
        snapshot=replace(fresh_claim.snapshot, source_result_version=1),
    )
    with pytest.raises(OrientationFailure) as mismatch:
        await repo.complete(
            forged,
            corrected_input_version=3,
            result_batch_id=result_batch_id,
            result_version=3,
            now=NOW,
        )
    assert mismatch.value.code == OrientationErrorCode.CLAIM_CONFLICT.value

    await RetentionRepository(create_session_factory(engine)).request_early_delete(
        batch_id, now=NOW
    )
    await repo.invalidate_batch(batch_id, now=NOW)
    with pytest.raises(OrientationFailure) as deleted:
        await repo.fail(
            fresh_claim,
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
        lambda: repo.complete(
            claim,
            corrected_input_version=3,
            result_batch_id=result_batch_id,
            result_version=3,
            now=NOW,
        ),
        lambda: repo.fail(
            claim,
            state=RecoveryState.FAILED,
            error_code="orientation_failed",
            now=NOW,
        ),
    ):
        with pytest.raises(OrientationFailure):
            await operation()


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
    live_issue = await repo.issue(
        binding(batch_id, expires_at=NOW + timedelta(hours=1)), now=NOW
    )
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE retention SET data_tombstone = 'data-deleting' WHERE batch_id = :batch"),
            {"batch": batch_id},
        )
    with pytest.raises(OrientationFailure):
        await repo.resolve(live_issue.token, now=NOW)


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
    with pytest.raises(OrientationFailure) as missing_result:
        await repo.complete(
            claim,
            corrected_input_version=3,
            result_batch_id="22222222-2222-4222-8222-222222222222",
            result_version=3,
            now=NOW,
        )
    assert missing_result.value.code == OrientationErrorCode.CLAIM_CONFLICT.value
