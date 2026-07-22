from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from pypdf import PdfWriter
from sqlalchemy import func, select

from ocr_mcp_server.domain.errors import (
    FileIntakeFailure,
    RetentionErrorCode,
    RetentionFailure,
)
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
from ocr_mcp_server.domain.files import IncomingFile
from ocr_mcp_server.services.file_intake import FileIntakeService
from ocr_mcp_server.services.file_storage import BatchLockLease, FileStorage
from ocr_mcp_server.services.file_validation import FileValidator
from ocr_mcp_server.services.retention import OwnedBatchRootDeleter, RetentionService
from ocr_mcp_server.settings import AppSettings


NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _intake_service(data_root: Path, guards) -> FileIntakeService:
    return FileIntakeService(
        storage=FileStorage(data_root),
        content_write_guards=guards,
        validator=FileValidator(max_pages=10, max_image_pixels=1_000),
        max_files=20,
        max_file_size_bytes=1_000_000,
        max_batch_size_bytes=1_000_000,
        now_factory=lambda: NOW,
    )


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
                storage_key=batch_id + "/artifact-" + "a" * 64 + ".zip",
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

        def delete(
            self, root: Path, selected_batch_id: str, *, tombstone_name=None
        ) -> None:
            if root == artifact_root and not self.failed:
                self.failed = True
                raise RetentionFailure(RetentionErrorCode.CLEANUP_FAILED)
            self.real.delete(
                root, selected_batch_id, tombstone_name=tombstone_name
            )

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
    published_archives = list(artifact_root.rglob("*.zip"))
    assert published_archives
    assert all(path.stat().st_size == 0 for path in published_archives)


@pytest.mark.asyncio
async def test_content_cleanup_retires_the_batch_lock_with_a_content_free_marker(
    tmp_path: Path, retention_repository,
) -> None:
    repository, tasks, _, _ = retention_repository
    batch_id, _ = await _create_batch(tasks, "lock-placeholder")
    data_root = (tmp_path / "data").absolute()
    artifact_root = (tmp_path / "artifacts").absolute()
    (data_root / batch_id).mkdir(parents=True)
    (artifact_root / batch_id).mkdir(parents=True)
    lock_path = data_root / ".locks" / f"{batch_id}.lock"
    lock_path.parent.mkdir()
    lock_path.write_bytes(b"private bytes must not survive retention")

    result = await RetentionService(repository, data_root, artifact_root).run_once(
        "worker", now=NOW + timedelta(hours=24), lease_seconds=60, limit=1
    )

    assert (result.content_deleted, result.failed) == (1, 0)
    assert lock_path.exists()
    assert lock_path.read_bytes() == b"\x01"


@pytest.mark.asyncio
async def test_retired_lock_name_swap_preserves_replacement_and_fails_closed(
    tmp_path: Path, retention_repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, tasks, _, _ = retention_repository
    batch_id, _ = await _create_batch(tasks, "lock-name-swap")
    data_root = (tmp_path / "data").absolute()
    artifact_root = (tmp_path / "artifacts").absolute()
    (data_root / batch_id).mkdir(parents=True)
    (artifact_root / batch_id).mkdir(parents=True)
    lock_path = data_root / ".locks" / f"{batch_id}.lock"
    moved = lock_path.with_name("moved-original.lock")
    original_retire = BatchLockLease.retire

    def swap_before_retire(lease):
        __import__("os").rename(lock_path, moved)
        lock_path.write_bytes(b"replacement")
        original_retire(lease)

    monkeypatch.setattr(BatchLockLease, "retire", swap_before_retire)
    with pytest.raises(RetentionFailure):
        await RetentionService(repository, data_root, artifact_root).delete_task(
            batch_id, now=NOW, worker_id="cleanup"
        )

    assert lock_path.read_bytes() == b"replacement"
    assert moved.read_bytes() == b"\x01"
    assert (await repository.get(batch_id)).content_deleted_at is None


@pytest.mark.asyncio
async def test_partial_scrub_resumes_the_persisted_tombstone_before_db_unavailable(
    tmp_path: Path, retention_repository,
) -> None:
    repository, tasks, sessions, _ = retention_repository
    batch_id, file_id = await _create_batch(tasks, "scrub-resume")
    await _insert_artifact(sessions, batch_id, file_id)
    data_root = (tmp_path / "data").absolute()
    artifact_root = (tmp_path / "artifacts").absolute()
    batch = data_root / batch_id
    batch.mkdir(parents=True)
    (batch / "one.bin").write_bytes(b"one-private")
    (batch / "two.bin").write_bytes(b"two-private")
    (artifact_root / batch_id).mkdir(parents=True)

    class FailSecondScrub(OwnedBatchRootDeleter):
        calls = 0

        def _scrub_open_regular(self, descriptor, expected, path):
            self.calls += 1
            if self.calls == 2:
                raise RetentionFailure(RetentionErrorCode.CLEANUP_FAILED)
            return super()._scrub_open_regular(descriptor, expected, path)

    failed = RetentionService(
        repository, data_root, artifact_root, deleter=FailSecondScrub()
    )
    assert (await failed.run_once(
        "worker", now=NOW + timedelta(hours=24), lease_seconds=60, limit=1
    )).failed == 1

    async with sessions() as session:
        artifact = await session.get(ArtifactRecord, "artifact-" + "a" * 64)
        assert artifact.available is True
    remaining = [path for path in data_root.rglob("*.bin")]
    assert any(path.stat().st_size > 0 for path in remaining)

    recovered = RetentionService(repository, data_root, artifact_root)
    assert (await recovered.run_once(
        "worker", now=NOW + timedelta(hours=24), lease_seconds=60, limit=1
    )).content_deleted == 1
    assert all(path.stat().st_size == 0 for path in data_root.rglob("*.bin"))


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


@pytest.mark.asyncio
async def test_early_deletion_resumes_metadata_after_first_purge_failure(
    tmp_path: Path, retention_repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, tasks, sessions, _ = retention_repository
    batch_id, file_id = await _create_batch(tasks, "early-resume")
    await _insert_artifact(sessions, batch_id, file_id)
    data_root = (tmp_path / "data").absolute()
    artifact_root = (tmp_path / "artifacts").absolute()
    (data_root / batch_id).mkdir(parents=True)
    (artifact_root / batch_id).mkdir(parents=True)
    service = RetentionService(repository, data_root, artifact_root)
    original_purge = repository.purge_metadata
    attempts = 0

    async def fail_first_purge(claim, *, now):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RetentionFailure(RetentionErrorCode.METADATA_PURGE_FAILED)
        return await original_purge(claim, now=now)

    monkeypatch.setattr(repository, "purge_metadata", fail_first_purge)
    with pytest.raises(RetentionFailure) as first:
        await service.delete_task(batch_id, now=NOW, worker_id="trusted")
    assert first.value.code == RetentionErrorCode.METADATA_PURGE_FAILED.value
    assert (await repository.get(batch_id)).content_deleted_at == NOW

    assert await service.delete_task(
        batch_id, now=NOW + timedelta(seconds=1), worker_id="trusted"
    ) is True
    assert attempts == 2
    assert await repository.get(batch_id) is None


@pytest.mark.asyncio
async def test_cleanup_barrier_wins_before_intake_and_no_new_bytes_land(
    tmp_path: Path, retention_repository,
) -> None:
    repository, tasks, _, _ = retention_repository
    batch_id, _ = await _create_batch(tasks, "cleanup-wins")
    data_root = (tmp_path / "data").absolute()
    artifact_root = (tmp_path / "artifacts").absolute()
    (data_root / batch_id).mkdir(parents=True)
    (artifact_root / batch_id).mkdir(parents=True)
    cleanup_holds_lock = asyncio.Event()
    allow_cleanup = asyncio.Event()

    class GatedRepository:
        def __getattr__(self, name):
            return getattr(repository, name)

        async def prepare_tombstone(self, claim, *, root_kind, now):
            if root_kind == "data":
                cleanup_holds_lock.set()
                await allow_cleanup.wait()
            return await repository.prepare_tombstone(
                claim, root_kind=root_kind, now=now
            )

    cleanup = asyncio.create_task(
        RetentionService(GatedRepository(), data_root, artifact_root).delete_task(
            batch_id, now=NOW, worker_id="cleanup"
        )
    )
    await cleanup_holds_lock.wait()

    async def content():
        yield _pdf_bytes()

    intake = asyncio.create_task(
        _intake_service(data_root, repository).ingest_upload(
            batch_id,
            IncomingFile("late.pdf", "application/pdf", content()),
        )
    )
    await asyncio.sleep(0)
    assert intake.done() is False
    allow_cleanup.set()

    assert await cleanup is True
    with pytest.raises((FileIntakeFailure, RetentionFailure)):
        await intake
    assert not list(data_root.rglob("late.pdf"))
    assert not list((data_root / batch_id).glob("input/*"))


@pytest.mark.asyncio
async def test_intake_allows_a_canonical_batch_that_has_no_retention_row(
    tmp_path: Path, retention_repository,
) -> None:
    repository, _, _, _ = retention_repository
    data_root = (tmp_path / "data").absolute()
    batch_id = str(uuid4())

    async def content():
        yield _pdf_bytes()

    stored = await _intake_service(data_root, repository).ingest_upload(
        batch_id,
        IncomingFile("fresh.pdf", "application/pdf", content()),
    )

    assert stored.path.exists()
    assert stored.path.read_bytes() == _pdf_bytes()


@pytest.mark.asyncio
async def test_intake_barrier_wins_first_and_cleanup_waits_then_removes_it(
    tmp_path: Path, retention_repository,
) -> None:
    repository, tasks, _, _ = retention_repository
    batch_id, _ = await _create_batch(tasks, "intake-wins")
    data_root = (tmp_path / "data").absolute()
    artifact_root = (tmp_path / "artifacts").absolute()
    artifact_root.joinpath(batch_id).mkdir(parents=True)
    intake_started = asyncio.Event()
    allow_intake = asyncio.Event()
    payload = _pdf_bytes()

    async def content():
        midpoint = len(payload) // 2
        yield payload[:midpoint]
        intake_started.set()
        await allow_intake.wait()
        yield payload[midpoint:]

    intake = asyncio.create_task(
        _intake_service(data_root, repository).ingest_upload(
            batch_id,
            IncomingFile("held.pdf", "application/pdf", content()),
        )
    )
    await intake_started.wait()
    cleanup = asyncio.create_task(
        RetentionService(repository, data_root, artifact_root).delete_task(
            batch_id, now=NOW, worker_id="cleanup"
        )
    )
    await asyncio.sleep(0)
    assert cleanup.done() is False

    allow_intake.set()
    stored = await intake
    assert stored.path.exists()
    assert await cleanup is True
    assert stored.path.exists() is False
    retained = [path for path in data_root.rglob("*") if path.is_file()]
    assert retained
    assert all(path.read_bytes() in {b"", b"\x01"} for path in retained)


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
    tmp_path: Path,
) -> None:
    root = (tmp_path / "owned").absolute()
    batch_id = str(uuid4())
    batch = root / batch_id
    batch.mkdir(parents=True)
    original = batch / "result.zip"
    original.write_bytes(b"owned-original")
    moved_paths: list[Path] = []

    class SwapAfterOpen(OwnedBatchRootDeleter):
        def _scrub_open_regular(self, descriptor, expected, path):
            moved = path.with_name("moved-original.zip")
            __import__("os").rename(path, moved)
            path.write_bytes(b"replacement")
            moved_paths.append(moved)
            return super()._scrub_open_regular(descriptor, expected, path)

    with pytest.raises(RetentionFailure) as caught:
        SwapAfterOpen().delete(root, batch_id)

    assert caught.value.code == RetentionErrorCode.CLEANUP_OWNERSHIP.value
    replacements = [path for path in root.rglob("result.zip")]
    assert len(replacements) == 1
    assert replacements[0].read_bytes() == b"replacement"
    assert moved_paths[0].read_bytes() == b""


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
    nested = root / batch_id / "nested"
    nested.mkdir(parents=True)
    (nested / "original.bin").write_bytes(b"owned")
    moved_paths: list[Path] = []

    class SwapOpenedDirectory(OwnedBatchRootDeleter):
        def _open_directory(self, path, **kwargs):
            pinned = super()._open_directory(path, **kwargs)
            if path.name == "nested" and not moved_paths:
                moved = path.with_name("held-original")
                __import__("os").rename(path, moved)
                path.mkdir()
                (path / "replacement.bin").write_bytes(b"replacement")
                moved_paths.append(moved)
            return pinned

    with pytest.raises(RetentionFailure):
        SwapOpenedDirectory().delete(root, batch_id)
    assert moved_paths[0].joinpath("original.bin").read_bytes() == b"owned"
    replacements = list(root.rglob("replacement.bin"))
    assert len(replacements) == 1
    assert replacements[0].read_bytes() == b"replacement"


def test_owned_root_scrubbing_never_unlinks_or_rmdirs_descendant_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "owned").absolute()
    batch_id = str(uuid4())
    nested = root / batch_id / "intermediate" / "nested"
    nested.mkdir(parents=True)
    (nested / "content.bin").write_bytes(b"private business bytes")

    monkeypatch.setattr(
        "ocr_mcp_server.services.retention.os.unlink",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unlink forbidden")),
    )
    monkeypatch.setattr(
        "ocr_mcp_server.services.retention.os.rmdir",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rmdir forbidden")),
    )

    OwnedBatchRootDeleter().delete(root, batch_id)

    assert not (root / batch_id).exists()
    remaining_files = [path for path in root.rglob("*") if path.is_file()]
    assert remaining_files
    assert all(path.read_bytes() == b"" for path in remaining_files)


def test_owned_root_isolation_preserves_root_replacement_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "owned").absolute()
    batch_id = str(uuid4())
    target = root / batch_id
    target.mkdir(parents=True)
    (target / "original.bin").write_bytes(b"owned")
    moved_original = root / "moved-original"
    real_rename = __import__("os").rename
    swapped = False

    def swap_root_before_isolation(source, destination, *args, **kwargs):
        nonlocal swapped
        if Path(source) == target and not swapped:
            swapped = True
            real_rename(target, moved_original)
            target.mkdir()
            (target / "replacement.bin").write_bytes(b"replacement")
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(
        "ocr_mcp_server.services.retention.os.rename", swap_root_before_isolation
    )
    with pytest.raises(RetentionFailure):
        OwnedBatchRootDeleter().delete(root, batch_id)

    assert (target / "replacement.bin").read_bytes() == b"replacement"
    assert (moved_original / "original.bin").read_bytes() == b"owned"


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
