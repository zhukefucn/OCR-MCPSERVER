from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ocr_mcp_server.domain.errors import InputValidationError
from ocr_mcp_server.domain.files import StoredFile, SupportedMediaType
from ocr_mcp_server.infra.database import (
    create_database_engine,
    create_session_factory,
    initialize_schema,
)
from ocr_mcp_server.infra.retention_repository import RetentionRepository
from ocr_mcp_server.infra.task_repository import TaskRepository
from ocr_mcp_server.infra.upload_repository import UploadRepository
from ocr_mcp_server.services.file_storage import FileStorage
from ocr_mcp_server.services.staging_retention import StagingUploadRetention


@pytest.mark.asyncio
async def test_upload_mapping_is_durable_and_idempotent(tmp_path: Path) -> None:
    engine = create_database_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'uploads.sqlite3').as_posix()}"
    )
    await initialize_schema(engine)
    repository = UploadRepository(create_session_factory(engine))
    file_id = str(uuid4())
    storage_batch_id = str(uuid4())
    stored = StoredFile(
        file_id=file_id,
        path=tmp_path / f"{file_id}.pdf",
        sha256="a" * 64,
        size_bytes=10,
        media_type=SupportedMediaType.PDF,
        extension=".pdf",
        page_count=2,
        width=None,
        height=None,
    )

    created = await repository.register(
        stored,
        storage_batch_id=storage_batch_id,
        idempotency_key="upload-1",
        created_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    replayed = await repository.get_by_idempotency_key("upload-1")
    resolved = await repository.get(file_id)

    assert created == replayed == resolved
    assert resolved is not None
    assert resolved.storage_batch_id == storage_batch_id
    assert resolved.page_count == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_staging_upload_is_deleted_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "restart.sqlite3"
    data_root = (tmp_path / "data").absolute()
    data_root.mkdir()
    engine = create_database_engine(
        f"sqlite+aiosqlite:///{database.as_posix()}"
    )
    await initialize_schema(engine)
    repository = UploadRepository(create_session_factory(engine))
    file_id = str(uuid4())
    storage_batch_id = str(uuid4())
    batch_root = data_root / storage_batch_id
    batch_root.mkdir()
    (batch_root / "owned.bin").write_bytes(b"expired")
    created_at = datetime(2026, 7, 22, tzinfo=UTC)
    await repository.register(
        StoredFile(
            file_id=file_id,
            path=batch_root / "owned.bin",
            sha256="b" * 64,
            size_bytes=7,
            media_type=SupportedMediaType.PDF,
            extension=".pdf",
            page_count=1,
            width=None,
            height=None,
        ),
        storage_batch_id=storage_batch_id,
        idempotency_key=None,
        created_at=created_at,
        expires_at=created_at + timedelta(hours=1),
    )
    await engine.dispose()

    restarted_engine = create_database_engine(
        f"sqlite+aiosqlite:///{database.as_posix()}"
    )
    restarted_repository = UploadRepository(
        create_session_factory(restarted_engine)
    )
    service = StagingUploadRetention(
        restarted_repository,
        data_root,
        storage=FileStorage(data_root),
        marker_registry=RetentionRepository(
            create_session_factory(restarted_engine)
        ),
    )
    deleted = await service.run_once(
        now=datetime(2026, 7, 23, tzinfo=UTC), limit=10
    )

    assert deleted == 1
    assert not batch_root.exists()
    assert await restarted_repository.get(file_id) is None
    await restarted_engine.dispose()


@pytest.mark.asyncio
async def test_parse_adoption_and_expiry_claim_are_one_atomic_choice(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'race.sqlite3').as_posix()}"
    )
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    uploads = UploadRepository(sessions)
    tasks = TaskRepository(sessions)
    file_id = str(uuid4())
    storage_batch_id = str(uuid4())
    expires_at = datetime(2026, 7, 23, tzinfo=UTC)
    await uploads.register(
        StoredFile(
            file_id=file_id,
            path=tmp_path / f"{file_id}.pdf",
            sha256="c" * 64,
            size_bytes=8,
            media_type=SupportedMediaType.PDF,
            extension=".pdf",
            page_count=1,
            width=None,
            height=None,
        ),
        storage_batch_id=storage_batch_id,
        idempotency_key=None,
        created_at=expires_at - timedelta(hours=1),
        expires_at=expires_at,
    )

    adopted, claimed = await asyncio.gather(
        tasks.create_batch(
            "parse-race",
            (file_id,),
            require_available_uploads_at=expires_at - timedelta(microseconds=1),
        ),
        uploads.claim_expired(now=expires_at, limit=1),
        return_exceptions=True,
    )

    adoption_won = not isinstance(adopted, BaseException)
    cleanup_won = not isinstance(claimed, BaseException) and bool(claimed)
    assert adoption_won != cleanup_won
    if not adoption_won:
        assert isinstance(adopted, InputValidationError)
    await engine.dispose()


@pytest.mark.asyncio
async def test_staging_cleanup_claims_and_deletes_under_batch_marker_lock() -> None:
    events: list[str] = []
    upload = SimpleNamespace(
        file_id=str(uuid4()), storage_batch_id=str(uuid4())
    )

    class Uploads:
        async def list_expired_candidates(self, *, now, limit):
            return (upload,)

        async def claim_expired_one(self, file_id, *, now):
            events.append("claim")
            return upload

        async def delete_retired(self, file_id):
            events.append("mapping_deleted")
            return True

    class Lease:
        def retire(self):
            events.append("marker_retired")

    class Storage:
        @asynccontextmanager
        async def batch_lock(self, batch_id, **kwargs):
            assert kwargs["marker_registry"] == "markers"
            assert kwargs["allow_missing_marker"] is True
            assert kwargs["allow_retired"] is True
            events.append("lock_enter")
            try:
                yield Lease()
            finally:
                events.append("lock_exit")

    class Deleter:
        def delete(self, *args, **kwargs):
            events.append("content_deleted")

    service = StagingUploadRetention(
        Uploads(),
        Path("C:/staging").absolute(),
        storage=Storage(),
        marker_registry="markers",
        deleter=Deleter(),
    )
    assert await service.run_once(
        now=datetime(2026, 7, 23, tzinfo=UTC), limit=1
    ) == 1
    assert events == [
        "lock_enter",
        "claim",
        "content_deleted",
        "marker_retired",
        "mapping_deleted",
        "lock_exit",
    ]


@pytest.mark.asyncio
async def test_cancellation_does_not_release_staging_lock_before_delete_finishes() -> None:
    entered = asyncio.Event()
    exited = asyncio.Event()
    delete_started = threading.Event()
    delete_release = threading.Event()
    upload = SimpleNamespace(
        file_id=str(uuid4()), storage_batch_id=str(uuid4())
    )

    class Uploads:
        async def list_expired_candidates(self, **kwargs):
            return (upload,)

        async def claim_expired_one(self, *args, **kwargs):
            return upload

        async def delete_retired(self, *args, **kwargs):
            return True

    class Lease:
        def retire(self):
            return None

    class Storage:
        @asynccontextmanager
        async def batch_lock(self, *args, **kwargs):
            entered.set()
            try:
                yield Lease()
            finally:
                exited.set()

    class Deleter:
        def delete(self, *args, **kwargs):
            delete_started.set()
            assert delete_release.wait(timeout=5)

    service = StagingUploadRetention(
        Uploads(),
        Path("C:/staging").absolute(),
        storage=Storage(),
        marker_registry=object(),
        deleter=Deleter(),
    )
    cleanup = asyncio.create_task(
        service.run_once(
            now=datetime(2026, 7, 23, tzinfo=UTC), limit=1
        )
    )
    await entered.wait()
    assert await asyncio.to_thread(delete_started.wait, 2)
    cleanup.cancel()
    await asyncio.sleep(0)
    assert not exited.is_set()
    delete_release.set()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    assert exited.is_set()
