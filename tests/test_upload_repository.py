from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from ocr_mcp_server.domain.files import StoredFile, SupportedMediaType
from ocr_mcp_server.infra.database import (
    create_database_engine,
    create_session_factory,
    initialize_schema,
)
from ocr_mcp_server.infra.upload_repository import UploadRepository
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
    service = StagingUploadRetention(restarted_repository, data_root)
    deleted = await service.run_once(
        now=datetime(2026, 7, 23, tzinfo=UTC), limit=10
    )

    assert deleted == 1
    assert not batch_root.exists()
    assert await restarted_repository.get(file_id) is None
    await restarted_engine.dispose()
