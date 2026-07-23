"""Safe resolution of public artifact identifiers to retained ZIP files."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from ..api.gateway import GatewayNotFound
from ..domain.models import utc_now


def public_artifact_id(internal_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"ocr-artifact:{internal_id}"))


@dataclass(frozen=True, slots=True)
class ArtifactDownload:
    path: Path
    media_type: str = "application/zip"


class ArtifactDownloadService:
    def __init__(self, repository, artifact_root: Path, *, now_factory=utc_now):
        self._repository = repository
        self._root = Path(artifact_root).absolute()
        self._now_factory = now_factory

    async def resolve(self, artifact_id: str) -> ArtifactDownload:
        try:
            if str(UUID(artifact_id)) != artifact_id:
                raise ValueError
            item = await self._repository.get_by_public_id(artifact_id)
            if (
                item is None
                or not item.available
                or item.expires_at <= self._now_factory()
            ):
                raise ValueError
            path = (self._root / item.storage_key).resolve()
            if path.parent == self._root or self._root not in path.parents:
                raise ValueError
            if path.suffix != ".zip" or not path.is_file():
                raise ValueError
            return ArtifactDownload(path)
        except (ValueError, OSError):
            raise GatewayNotFound() from None


__all__ = ["ArtifactDownload", "ArtifactDownloadService", "public_artifact_id"]
