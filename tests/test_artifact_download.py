from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ocr_mcp_server.api.gateway import GatewayNotFound
from ocr_mcp_server.services.artifact_download import (
    ArtifactDownloadService,
    public_artifact_id,
)


@pytest.mark.asyncio
async def test_artifact_download_resolves_only_available_unexpired_owned_zip(tmp_path: Path):
    internal_id = "artifact-" + "a" * 64
    path = tmp_path / "batch" / f"{internal_id}.zip"
    path.parent.mkdir()
    path.write_bytes(b"zip")
    now = datetime(2026, 7, 23, tzinfo=UTC)
    snapshot = SimpleNamespace(
        artifact_id=internal_id,
        storage_key=f"batch/{internal_id}.zip",
        available=True,
        expires_at=now + timedelta(hours=1),
    )

    class Repository:
        async def get_by_public_id(self, value):
            assert value == public_artifact_id(internal_id)
            return snapshot

    service = ArtifactDownloadService(Repository(), tmp_path, now_factory=lambda: now)
    resolved = await service.resolve(public_artifact_id(internal_id))
    assert resolved.path == path

    snapshot.storage_key = "../secret.zip"
    with pytest.raises(GatewayNotFound):
        await service.resolve(public_artifact_id(internal_id))
