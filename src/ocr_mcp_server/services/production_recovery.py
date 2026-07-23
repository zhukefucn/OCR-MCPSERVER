"""Production adapters for explicit whole-page orientation recovery."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
import tempfile
from uuid import uuid4

from PIL import Image
from pypdf import PdfReader

from ..domain.files import IncomingFile, StoredFile, SupportedMediaType
from ..domain.models import BatchStatus, utc_now
from ..domain.orientation import OrientationEvidence
from ..domain.secondary_ocr import (
    CandidateReference,
    ImageCandidate,
    MinerUImageFormat,
    OrthogonalAngle,
)
from .orientation_recovery import (
    FullRecoveryPipelineSubmission,
    OrientationCorrectionRequest,
    OrientationDetectionRequest,
)


class ProductionPageOrientationDetector:
    """Prefer PDF rotation metadata, then use the single Paddle owner on pixels."""

    def __init__(
        self,
        uploads,
        storage,
        paddle,
        *,
        max_file_size_bytes: int,
        max_image_pixels: int,
        page_renderer=None,
    ) -> None:
        self._uploads = uploads
        self._storage = storage
        self._paddle = paddle
        self._max_file_size_bytes = max_file_size_bytes
        self._max_image_pixels = max_image_pixels
        self._page_renderer = page_renderer or _render_pdf_page

    async def detect(
        self, request: OrientationDetectionRequest
    ) -> tuple[OrientationEvidence, ...]:
        upload = await self._uploads.get(request.file_id)
        if upload is None or upload.page_count != request.page_count:
            raise ValueError("orientation source unavailable")
        stored = await self._storage.resolve_stored(
            upload.storage_batch_id,
            request.file_id,
            expected_page_count=request.page_count,
            max_file_size_bytes=self._max_file_size_bytes,
        )
        if stored.media_type is SupportedMediaType.PDF:
            angles = await asyncio.to_thread(
                _pdf_metadata_corrections, stored.path, request.page_count
            )
        else:
            angles = (OrthogonalAngle.DEG_0,)
        evidence: list[OrientationEvidence] = []
        with tempfile.TemporaryDirectory(prefix="ocr-orientation-") as temporary:
            temporary_root = Path(temporary)
            for page in request.pages:
                metadata_angle = angles[page - 1]
                if metadata_angle is not OrthogonalAngle.DEG_0:
                    evidence.append(
                        OrientationEvidence(
                            page,
                            metadata_angle,
                            1.0,
                            "metadata_rotation",
                        )
                    )
                    continue
                if stored.media_type is SupportedMediaType.PDF:
                    image_path = temporary_root / f"page-{page}.png"
                    await asyncio.to_thread(
                        self._page_renderer,
                        stored.path,
                        page,
                        image_path,
                        self._max_image_pixels,
                    )
                else:
                    image_path = stored.path
                candidate = await asyncio.to_thread(
                    _orientation_candidate,
                    image_path,
                    request.file_id,
                    page,
                    self._max_file_size_bytes,
                    self._max_image_pixels,
                )
                classification = await self._paddle.classify_orientation(candidate)
                angle = classification.angle
                confidence = classification.confidence
                evidence.append(
                    OrientationEvidence(
                        page,
                        angle,
                        float(confidence),
                        "paddle_orientation"
                        if confidence > 0
                        else "paddle_uncertain",
                    )
                )
        return tuple(evidence)


class MappedRecoveryStorage:
    """Resolve task file IDs through the durable staging-upload mapping."""

    def __init__(self, uploads, tasks, storage) -> None:
        self._uploads = uploads
        self._tasks = tasks
        self._storage = storage

    @asynccontextmanager
    async def batch_lock(self, batch_id: str, **kwargs):
        files = await self._tasks.list_batch_files(batch_id)
        uploads = [await self._uploads.get(file.id) for file in files]
        if not files or any(upload is None for upload in uploads):
            raise ValueError("recovery inputs unavailable")
        async with AsyncExitStack() as stack:
            leases = {}
            storage_batch_ids = sorted(
                {
                    upload.storage_batch_id
                    for upload in uploads
                    if upload is not None
                }
            )
            for storage_batch_id in storage_batch_ids:
                leases[storage_batch_id] = await stack.enter_async_context(
                    self._storage.batch_lock(
                        storage_batch_id,
                        marker_registry=kwargs["marker_registry"],
                        allow_missing_marker=True,
                        allow_retired=False,
                    )
                )
            yield leases

    async def resolve_stored(
        self, batch_id: str, file_id: str, **kwargs
    ) -> StoredFile:
        del batch_id
        upload = await self._uploads.get(file_id)
        if upload is None:
            raise ValueError("recovery input unavailable")
        return await self._storage.resolve_stored(
            upload.storage_batch_id, file_id, **kwargs
        )


class MappedDocumentCorrector:
    """Run the immutable corrector in the source upload's owned storage root."""

    def __init__(
        self,
        *,
        delegate,
        uploads,
        now_factory=utc_now,
        retention_hours: int,
    ) -> None:
        self._delegate = delegate
        self._uploads = uploads
        self._now_factory = now_factory
        self._retention_hours = retention_hours

    async def correct(self, request: OrientationCorrectionRequest) -> StoredFile:
        upload = await self._uploads.get(request.file_id)
        if upload is None:
            raise ValueError("recovery input unavailable")
        lease = request.batch_lock[upload.storage_batch_id]
        mapped = replace(
            request,
            batch_id=upload.storage_batch_id,
            batch_lock=lease,
        )
        corrected = await self._delegate.correct(mapped)
        created_at = self._now_factory()
        await self._uploads.register(
            corrected,
            storage_batch_id=upload.storage_batch_id,
            idempotency_key=None,
            created_at=created_at,
            expires_at=created_at + timedelta(hours=self._retention_hours),
        )
        return corrected


class RecoveryPipelineRunner:
    """Adopt corrected bytes into a new durable upload and task batch."""

    def __init__(
        self,
        *,
        intake,
        uploads,
        tasks,
        orchestration,
        now_factory=utc_now,
        retention_hours: int,
        retention_options: dict[str, int] | None = None,
        id_factory=lambda: str(uuid4()),
    ) -> None:
        self._intake = intake
        self._uploads = uploads
        self._tasks = tasks
        self._orchestration = orchestration
        self._now_factory = now_factory
        self._retention_hours = retention_hours
        self._retention_options = dict(retention_options or {})
        self._id_factory = id_factory

    async def run(
        self,
        corrected: StoredFile,
        *,
        recovery_id: str,
        source_batch_id: str,
        source_result_version: int,
        corrected_input_version: int,
    ) -> FullRecoveryPipelineSubmission:
        del source_batch_id, source_result_version
        storage_batch_id = self._id_factory()
        accepted = await self._intake.ingest_upload(
            storage_batch_id,
            IncomingFile(
                f"{corrected.file_id}{corrected.extension}",
                corrected.media_type.value,
                _file_chunks(corrected.path),
            ),
        )
        if (
            accepted.sha256 != corrected.sha256
            or accepted.size_bytes != corrected.size_bytes
        ):
            raise ValueError("recovery adoption mismatch")
        created_at = self._now_factory()
        await self._uploads.register(
            accepted,
            storage_batch_id=storage_batch_id,
            idempotency_key=None,
            created_at=created_at,
            expires_at=created_at + timedelta(hours=self._retention_hours),
            adopted_source_file_id=corrected.file_id,
            result_version=corrected_input_version,
        )
        result = await self._tasks.create_batch(
            f"recovery:{recovery_id}",
            (accepted.file_id,),
            require_available_uploads_at=created_at,
            **self._retention_options,
        )
        if len(result.files) != 1:
            raise ValueError("recovery batch is invalid")
        batch_accepted = await self._uploads.get(result.files[0].id)
        if (
            batch_accepted is None
            or batch_accepted.adopted_source_file_id != corrected.file_id
            or batch_accepted.result_version != corrected_input_version
        ):
            raise ValueError("recovery batch adoption mismatch")
        if result.created:
            self._orchestration.notify_work()
        return FullRecoveryPipelineSubmission(
            batch_id=result.batch.id,
            status=BatchStatus.QUEUED,
            result_version=batch_accepted.result_version,
            adopted_source_file_id=batch_accepted.adopted_source_file_id,
            accepted_input_file_id=batch_accepted.file_id,
            accepted_input_sha256=batch_accepted.sha256,
            accepted_input_size_bytes=batch_accepted.size_bytes,
        )

    async def reconcile(
        self, recovery_id: str
    ) -> FullRecoveryPipelineSubmission | None:
        result = await self._tasks.get_by_idempotency_key(
            f"recovery:{recovery_id}"
        )
        if result is None or len(result.files) != 1:
            return None
        accepted = await self._uploads.get(result.files[0].id)
        if (
            accepted is None
            or accepted.adopted_source_file_id is None
            or accepted.result_version is None
        ):
            return None
        return FullRecoveryPipelineSubmission(
            batch_id=result.batch.id,
            status=BatchStatus.QUEUED,
            result_version=accepted.result_version,
            adopted_source_file_id=accepted.adopted_source_file_id,
            accepted_input_file_id=accepted.file_id,
            accepted_input_sha256=accepted.sha256,
            accepted_input_size_bytes=accepted.size_bytes,
        )


async def _file_chunks(path: Path):
    handle = await asyncio.to_thread(path.open, "rb")
    try:
        while chunk := await asyncio.to_thread(handle.read, 64 * 1024):
            yield chunk
    finally:
        await asyncio.to_thread(handle.close)


def _pdf_metadata_corrections(
    path: Path, expected_pages: int
) -> tuple[OrthogonalAngle, ...]:
    reader = PdfReader(path, strict=True)
    if reader.is_encrypted or len(reader.pages) != expected_pages:
        raise ValueError("invalid recovery PDF")
    corrections = []
    for page in reader.pages:
        rotation = page.get("/Rotate", 0)
        if (
            isinstance(rotation, bool)
            or not isinstance(rotation, int)
            or rotation % 90 != 0
        ):
            raise ValueError("invalid page rotation")
        corrections.append(OrthogonalAngle((-rotation) % 360))
    return tuple(corrections)


def _orientation_candidate(
    path: Path,
    file_id: str,
    page_number: int,
    max_file_size_bytes: int,
    max_image_pixels: int,
) -> ImageCandidate:
    content = path.read_bytes()
    if not content or len(content) > max_file_size_bytes:
        raise ValueError("orientation image is invalid")
    with Image.open(path) as image:
        image.load()
        width, height = image.size
        if width < 1 or height < 1 or width * height > max_image_pixels:
            raise ValueError("orientation image is invalid")
        image_format = {
            "PNG": MinerUImageFormat.PNG,
            "JPEG": MinerUImageFormat.JPEG,
        }.get(image.format or "")
    if image_format is None:
        raise ValueError("orientation image is invalid")
    digest = sha256(content).hexdigest()
    return ImageCandidate(
        candidate_id=f"orientation-{page_number}-{digest[:16]}",
        file_task_id=file_id,
        result_version=1,
        sha256=digest,
        size_bytes=len(content),
        image_format=image_format,
        width=width,
        height=height,
        primary_path=path,
        alias_paths=(path,),
        references=(CandidateReference.standalone_input(),),
        node_type_hints=(),
    )


def _render_pdf_page(
    source: Path,
    page_number: int,
    target: Path,
    max_image_pixels: int,
) -> None:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise ValueError("PDF page renderer unavailable") from None
    document = pdfium.PdfDocument(str(source))
    try:
        page = document[page_number - 1]
        try:
            bitmap = page.render(scale=2)
            try:
                image = bitmap.to_pil()
                try:
                    if image.width * image.height > max_image_pixels:
                        raise ValueError("rendered page is too large")
                    image.save(target, format="PNG")
                finally:
                    image.close()
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()


__all__ = [
    "MappedDocumentCorrector",
    "MappedRecoveryStorage",
    "ProductionPageOrientationDetector",
    "RecoveryPipelineRunner",
]
