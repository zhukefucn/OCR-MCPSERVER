from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from ocr_mcp_server.domain.errors import RetentionErrorCode, RetentionFailure
from ocr_mcp_server.domain.retention import RetentionPhase
from ocr_mcp_server.infra.database import (
    create_database_engine,
    create_session_factory,
    initialize_schema,
)
from ocr_mcp_server.infra.retention_repository import RetentionRepository
from ocr_mcp_server.infra.task_models import (
    ArtifactRecord,
    BatchRecord,
    FileTaskRecord,
    ReplacementAuditMetadataRecord,
    StageEventRecord,
)
from ocr_mcp_server.infra.task_repository import TaskRepository
from ocr_mcp_server.services.retention import OwnedBatchRootDeleter, RetentionService
from ocr_mcp_server.settings import AppSettings


NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _db_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest_asyncio.fixture
async def retention_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from ocr_mcp_server.infra import task_repository as task_repository_module

    monkeypatch.setattr(task_repository_module, "utc_now", lambda: NOW)
    engine = create_database_engine(_db_url(tmp_path / "retention.sqlite3"))
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    tasks = TaskRepository(sessions)
    repository = RetentionRepository(sessions)
    try:
        yield repository, tasks, sessions, engine
    finally:
        await engine.dispose()


async def _create_batch(tasks: TaskRepository, key: str):
    file_id = str(uuid4())
    created = await tasks.create_batch(
        key,
        [file_id],
        input_retention_hours=24,
        intermediate_retention_hours=12,
        result_retention_hours=6,
        audit_metadata_retention_days=30,
    )
    return created.batch.id, file_id


@pytest.mark.asyncio
async def test_due_boundaries_are_exact_and_claims_are_bounded_and_deterministic(
    retention_repository,
) -> None:
    repository, tasks, _, _ = retention_repository
    first, _ = await _create_batch(tasks, "first")
    second, _ = await _create_batch(tasks, "second")

    state = await repository.get(first)
    assert state.content_due_at == NOW + timedelta(hours=24)
    assert state.metadata_due_at == NOW + timedelta(days=30)
    assert await repository.claim_due(
        "worker", now=state.content_due_at - timedelta(microseconds=1),
        lease_seconds=60, limit=1,
    ) == ()

    claim = await repository.claim_due(
        "worker", now=state.content_due_at, lease_seconds=60, limit=1
    )
    assert len(claim) == 1
    assert claim[0].batch_id == min(first, second)
    assert claim[0].phase is RetentionPhase.CONTENT
    assert claim[0].lease_expires_at == state.content_due_at + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_claim_is_exclusive_until_lease_expiry_then_recoverable(
    retention_repository,
) -> None:
    repository, tasks, _, _ = retention_repository
    batch_id, _ = await _create_batch(tasks, "lease")
    due = NOW + timedelta(hours=24)

    first = (await repository.claim_due(
        "one", now=due, lease_seconds=10, limit=1
    ))[0]
    assert await repository.claim_due(
        "two", now=due + timedelta(seconds=9), lease_seconds=10, limit=1
    ) == ()
    reclaimed = (await repository.claim_due(
        "two", now=due + timedelta(seconds=10), lease_seconds=10, limit=1
    ))[0]

    assert reclaimed.batch_id == batch_id
    assert reclaimed.claim_token != first.claim_token
    assert reclaimed.attempt == 2


@pytest.mark.asyncio
async def test_concurrent_cleanup_claims_select_a_batch_only_once(
    retention_repository,
) -> None:
    repository, tasks, _, _ = retention_repository
    batch_id, _ = await _create_batch(tasks, "concurrent")
    due = NOW + timedelta(hours=24)

    claims = await asyncio.gather(
        repository.claim_due("one", now=due, lease_seconds=60, limit=1),
        repository.claim_due("two", now=due, lease_seconds=60, limit=1),
    )

    flattened = tuple(claim for group in claims for claim in group)
    assert len(flattened) == 1
    assert flattened[0].batch_id == batch_id


async def _insert_artifact(
    sessions, batch_id: str, file_id: str, *, with_audit: bool = False
) -> None:
    async with sessions() as session:
        session.add(
            ArtifactRecord(
                id="artifact-" + "a" * 64,
                file_id=file_id,
                batch_id=batch_id,
                source_version=1,
                result_version=1,
                storage_key="artifact-" + "a" * 64 + ".zip",
                media_type="application/zip",
                size_bytes=1,
                sha256="b" * 64,
                manifest_sha256="c" * 64,
                audit_metadata_sha256="d" * 64,
                audit_record_count=1 if with_audit else 0,
                created_at=NOW,
                expires_at=NOW + timedelta(hours=24),
                available=True,
                deleted_at=None,
                version=1,
            )
        )
        if with_audit:
            session.add(
                ReplacementAuditMetadataRecord(
                    audit_id="audit-" + "e" * 64,
                    artifact_id="artifact-" + "a" * 64,
                    record_id="secondary-" + "f" * 64,
                    candidate_id="candidate-" + "1" * 64,
                    file_id=file_id,
                    batch_id=batch_id,
                    source_version=1,
                    output_version=1,
                    image_sha256="2" * 64,
                    decision="replaced",
                    reason="replaced_table",
                    kind="table",
                    angle=0,
                    confidence=0.9,
                    engine="pp_structure_v3",
                    model_versions_json="{}",
                    timestamp=NOW,
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_partial_content_failure_is_retryable_and_does_not_mark_db_unavailable(
    tmp_path: Path, retention_repository,
) -> None:
    repository, tasks, sessions, _ = retention_repository
    batch_id, file_id = await _create_batch(tasks, "partial")
    await _insert_artifact(sessions, batch_id, file_id)
    data_root = (tmp_path / "data").absolute()
    artifact_root = (tmp_path / "artifacts").absolute()
    (data_root / batch_id / "input").mkdir(parents=True)
    (data_root / batch_id / "input" / "source.bin").write_bytes(b"source")
    (artifact_root / batch_id).mkdir(parents=True)
    (artifact_root / batch_id / "result.zip").write_bytes(b"result")

    class FailArtifactOnce:
        def __init__(self) -> None:
            self.failed = False
            self.real = OwnedBatchRootDeleter()

        def delete(self, root: Path, selected_batch_id: str) -> None:
            if root == artifact_root and not self.failed:
                self.failed = True
                raise RetentionFailure(RetentionErrorCode.CLEANUP_FAILED)
            self.real.delete(root, selected_batch_id)

    service = RetentionService(repository, data_root, artifact_root, deleter=FailArtifactOnce())
    result = await service.run_once(
        "worker", now=NOW + timedelta(hours=24), lease_seconds=60, limit=1
    )
    assert result.failed == 1
    assert not (data_root / batch_id).exists()
    assert (artifact_root / batch_id).exists()
    async with sessions() as session:
        artifact = await session.get(ArtifactRecord, "artifact-" + "a" * 64)
        assert artifact.available is True
        assert artifact.deleted_at is None

    retried = await service.run_once(
        "worker", now=NOW + timedelta(hours=24), lease_seconds=60, limit=1
    )
    assert (retried.content_deleted, retried.failed) == (1, 0)
    assert not (artifact_root / batch_id).exists()
    async with sessions() as session:
        artifact = await session.get(ArtifactRecord, "artifact-" + "a" * 64)
        assert artifact.available is False
        assert artifact.deleted_at == (NOW + timedelta(hours=24)).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_metadata_remains_until_exact_30_day_boundary_then_purges_all_task_rows(
    tmp_path: Path, retention_repository,
) -> None:
    repository, tasks, sessions, _ = retention_repository
    batch_id, file_id = await _create_batch(tasks, "purge")
    await _insert_artifact(sessions, batch_id, file_id, with_audit=True)
    assert await tasks.claim_next("pipeline", now=NOW, lease_seconds=60) is not None
    data_root = (tmp_path / "data").absolute()
    artifact_root = (tmp_path / "artifacts").absolute()
    (data_root / batch_id).mkdir(parents=True)
    (artifact_root / batch_id).mkdir(parents=True)
    service = RetentionService(repository, data_root, artifact_root)

    await service.run_once(
        "worker", now=NOW + timedelta(hours=24), lease_seconds=60, limit=1
    )
    async with sessions() as session:
        assert await session.scalar(
            select(func.count()).select_from(ReplacementAuditMetadataRecord)
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(StageEventRecord)
        ) == 1
    assert await repository.claim_due(
        "worker", now=NOW + timedelta(days=30) - timedelta(microseconds=1),
        lease_seconds=60, limit=1,
    ) == ()
    purged = await service.run_once(
        "worker", now=NOW + timedelta(days=30), lease_seconds=60, limit=1
    )
    assert purged.metadata_purged == 1

    async with sessions() as session:
        assert await session.get(BatchRecord, batch_id) is None
        assert await session.get(FileTaskRecord, file_id) is None
        assert await session.get(ArtifactRecord, "artifact-" + "a" * 64) is None
        assert await session.scalar(
            select(func.count()).select_from(ReplacementAuditMetadataRecord)
        ) == 0
        assert await session.scalar(
            select(func.count()).select_from(StageEventRecord)
        ) == 0


@pytest.mark.asyncio
async def test_early_deletion_runs_both_phases_and_is_idempotent(
    tmp_path: Path, retention_repository,
) -> None:
    repository, tasks, sessions, _ = retention_repository
    batch_id, file_id = await _create_batch(tasks, "early")
    await _insert_artifact(sessions, batch_id, file_id)
    data_root = (tmp_path / "data").absolute()
    artifact_root = (tmp_path / "artifacts").absolute()
    (data_root / batch_id).mkdir(parents=True)
    (artifact_root / batch_id).mkdir(parents=True)
    service = RetentionService(repository, data_root, artifact_root)

    deleted = await service.delete_task(batch_id, now=NOW, worker_id="trusted")
    repeated = await service.delete_task(batch_id, now=NOW, worker_id="trusted")

    assert deleted is True
    assert repeated is False
    assert not (data_root / batch_id).exists()
    assert not (artifact_root / batch_id).exists()
    async with sessions() as session:
        for model in (ArtifactRecord, FileTaskRecord, BatchRecord):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


def test_owned_root_deletion_rejects_unowned_ids_and_symlinks_but_missing_is_success(tmp_path: Path) -> None:
    root = (tmp_path / "owned").absolute()
    root.mkdir()
    deleter = OwnedBatchRootDeleter()

    for batch_id in ("", "../escape", str(uuid4()).upper()):
        with pytest.raises(RetentionFailure) as caught:
            deleter.delete(root, batch_id)
        assert caught.value.code == RetentionErrorCode.CLEANUP_OWNERSHIP.value
    deleter.delete((tmp_path / "missing-root").absolute(), str(uuid4()))

    if hasattr(Path, "symlink_to"):
        batch_id = str(uuid4())
        outside = tmp_path / "outside"
        outside.mkdir()
        planted = root / batch_id
        try:
            planted.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks are unavailable")
        with pytest.raises(RetentionFailure):
            deleter.delete(root, batch_id)
        assert outside.exists()


def test_owned_root_deletion_leaks_and_preserves_a_concurrent_name_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "owned").absolute()
    batch_id = str(uuid4())
    batch = root / batch_id
    batch.mkdir(parents=True)
    original = batch / "result.zip"
    original.write_bytes(b"owned-original")
    moved_original = batch / "moved-original.zip"
    real_rename = __import__("os").rename
    swapped = False

    def swap_before_stage(source, target, *args, **kwargs):
        nonlocal swapped
        if Path(source) == original and not swapped:
            swapped = True
            real_rename(original, moved_original)
            original.write_bytes(b"replacement")
        return real_rename(source, target, *args, **kwargs)

    monkeypatch.setattr("ocr_mcp_server.services.retention.os.rename", swap_before_stage)
    with pytest.raises(RetentionFailure) as caught:
        OwnedBatchRootDeleter().delete(root, batch_id)

    assert caught.value.code == RetentionErrorCode.CLEANUP_OWNERSHIP.value
    assert original.read_bytes() == b"replacement"
    assert moved_original.read_bytes() == b"owned-original"


def test_owned_root_deletion_discards_planted_exception_text_and_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "C:/client/private.pdf recognized business text"
    root = (tmp_path / "owned").absolute()
    root.mkdir()
    real_lstat = __import__("os").lstat

    def fail_target(path, *args, **kwargs):
        if Path(path).parent == root:
            raise OSError(secret)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr("ocr_mcp_server.services.retention.os.lstat", fail_target)
    with pytest.raises(RetentionFailure) as caught:
        OwnedBatchRootDeleter().delete(root, str(uuid4()))
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_owned_root_deletion_does_not_remove_directory_replaced_at_final_delete(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "owned").absolute()
    batch_id = str(uuid4())
    (root / batch_id).mkdir(parents=True)
    moved = root / "held-original"

    class SwapFinalDirectory(OwnedBatchRootDeleter):
        def _remove_empty_directory(self, staged: Path, expected):
            __import__("os").rename(staged, moved)
            staged.mkdir()
            return super()._remove_empty_directory(staged, expected)

    with pytest.raises(RetentionFailure):
        SwapFinalDirectory().delete(root, batch_id)
    assert moved.exists()
    assert any(path.name.startswith(".cleanup-") for path in root.iterdir())


def test_retention_settings_include_bounded_cleanup_defaults_and_reject_booleans() -> None:
    settings = AppSettings().retention
    assert settings.cleanup_batch_size == 25
    assert settings.cleanup_lease_seconds == 300
    assert settings.cleanup_interval_seconds == 300
    for field in ("cleanup_batch_size", "cleanup_lease_seconds", "cleanup_interval_seconds"):
        with pytest.raises(ValueError):
            AppSettings(retention={field: True})


def test_example_yaml_documents_cleanup_defaults() -> None:
    import yaml
    from ocr_mcp_server.settings import load_settings

    retention = load_settings(config_file=Path("config/example.yaml")).retention
    documented = yaml.safe_load(Path("config/example.yaml").read_text(encoding="utf-8"))["retention"]
    assert retention.cleanup_batch_size == 25
    assert retention.cleanup_lease_seconds == 300
    assert retention.cleanup_interval_seconds == 300
    assert documented["cleanup_batch_size"] == 25
    assert documented["cleanup_lease_seconds"] == 300
    assert documented["cleanup_interval_seconds"] == 300
