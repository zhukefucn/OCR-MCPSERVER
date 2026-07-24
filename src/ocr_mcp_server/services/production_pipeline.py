"""Thin production pipeline that sequences the already-validated OCR steps."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from ..domain import (
    MinerUParseRequest,
    SecondaryOCREngine,
    SecondaryResultState,
)
from ..domain.models import ProcessingStage, utc_now
from ..domain.progress import ProgressCounters, ProgressUnit
from .candidate_collection import collect_image_candidates
from .merge_publication import merge_and_publish
from .orientation_recovery import OrientationDetectionRequest
from .orchestration import (
    PipelineCancellation,
    PipelineErrorCode,
    PipelineFailure,
    PipelineFileIdentity,
    PipelineResult,
)


class ProductionFilePipeline:
    """Sequence MinerU, every-image Paddle detection, merge, and packaging."""

    def __init__(
        self,
        *,
        uploads,
        storage,
        mineru,
        paddle,
        packaging,
        data_root: Path,
        artifact_root: Path,
        max_file_size_bytes: int,
        max_image_pixels: int,
        structured_limits,
        result_retention_hours: int,
        orientation_detector=None,
        orientation_assessments=None,
        orientation_assessment_timeout_seconds: float = 90,
        orientation_assessment_lease_seconds: int = 100,
        collect_candidates=collect_image_candidates,
        merge_publication=merge_and_publish,
        now_factory=utc_now,
    ) -> None:
        if (
            isinstance(orientation_assessment_timeout_seconds, bool)
            or not isinstance(
                orientation_assessment_timeout_seconds, (int, float)
            )
            or orientation_assessment_timeout_seconds <= 0
            or type(orientation_assessment_lease_seconds) is not int
            or orientation_assessment_lease_seconds < 1
            or orientation_assessment_timeout_seconds
            >= orientation_assessment_lease_seconds
        ):
            raise ValueError("invalid orientation assessment bounds")
        self._uploads = uploads
        self._storage = storage
        self._mineru = mineru
        self._paddle = paddle
        self._packaging = packaging
        self._data_root = Path(data_root).absolute()
        self._artifact_root = Path(artifact_root).absolute()
        self._max_file_size_bytes = max_file_size_bytes
        self._max_image_pixels = max_image_pixels
        self._structured_limits = structured_limits
        self._result_retention_hours = result_retention_hours
        self._orientation_detector = orientation_detector
        self._orientation_assessments = orientation_assessments
        self._orientation_assessment_timeout_seconds = (
            orientation_assessment_timeout_seconds
        )
        self._orientation_assessment_lease_seconds = (
            orientation_assessment_lease_seconds
        )
        self._collect_candidates = collect_candidates
        self._merge_publication = merge_publication
        self._now_factory = now_factory

    async def run(
        self,
        file: PipelineFileIdentity,
        progress,
        cancellation: PipelineCancellation,
    ) -> PipelineResult:
        try:
            cancellation.checkpoint()
            upload = await self._uploads.get(file.file_id)
            if upload is None:
                raise PipelineFailure(
                    PipelineErrorCode.INPUT_INVALID, retryable=False
                )
            stored = await self._storage.resolve_stored(
                upload.storage_batch_id,
                file.file_id,
                expected_page_count=upload.page_count,
                max_file_size_bytes=self._max_file_size_bytes,
            )
            work_root = (
                self._data_root
                / file.batch_id
                / "intermediate"
                / file.file_id
            )
            mineru_root = work_root / "mineru"
            publication_root = work_root / "publication"

            await progress.report(ProcessingStage.MINERU_PARSING)
            result = await self._mineru.parse(
                MinerUParseRequest(
                    file_task_id=file.file_id,
                    source_path=stored.path,
                    upload_name=f"{file.file_id}{stored.extension}",
                    output_directory=mineru_root,
                )
            )
            cancellation.checkpoint()

            await progress.report(ProcessingStage.COLLECTING_IMAGES)
            collection = await asyncio.to_thread(
                self._collect_candidates,
                result,
                result_version=1,
                engine=SecondaryOCREngine.PP_STRUCTURE_V3,
                standalone_image=(
                    stored
                    if getattr(stored.media_type, "value", stored.media_type)
                    != "application/pdf"
                    else None
                ),
                max_image_pixels=self._max_image_pixels,
            )
            candidates = tuple(collection.candidates)
            await progress.report(ProcessingStage.CLASSIFYING_IMAGES)
            await progress.report(
                ProcessingStage.RECOGNIZING_IMAGES,
                ProgressCounters(0, len(candidates), ProgressUnit.ITEMS),
            )
            recognized = {}
            warned = False
            for index, candidate in enumerate(candidates, start=1):
                cancellation.checkpoint()
                value = await self._paddle.recognize(candidate)
                recognized[candidate.candidate_id] = value
                state = getattr(value.state, "value", value.state)
                warned = warned or state != SecondaryResultState.VALID.value
                await progress.report(
                    ProcessingStage.RECOGNIZING_IMAGES,
                    ProgressCounters(index, len(candidates), ProgressUnit.ITEMS),
                )

            await progress.report(ProcessingStage.MERGING)
            publication = await asyncio.to_thread(
                self._merge_publication,
                result,
                collection,
                recognized,
                publication_root=publication_root,
                output_version=2,
                timestamp=self._now_factory(),
                limits=self._structured_limits,
            )
            cancellation.checkpoint()
            created_at = self._now_factory()
            await self._packaging.run(
                result,
                publication,
                artifact_root=self._artifact_root,
                batch_id=file.batch_id,
                created_at=created_at,
                expires_at=created_at
                + timedelta(hours=self._result_retention_hours),
                progress=progress,
                cancellation=cancellation,
            )
            orientation_warning = await self._assess_orientation_once(
                file=file,
                page_count=upload.page_count,
            )
            warned = warned or orientation_warning
            cancellation.checkpoint()
            return (
                PipelineResult.success_with_warnings()
                if warned
                else PipelineResult.success()
            )
        except asyncio.CancelledError:
            raise
        except PipelineFailure:
            raise
        except Exception as exc:
            raise PipelineFailure(
                PipelineErrorCode.PROCESSING_FAILED,
                retryable=False,
                cause=exc,
            ) from None

    async def _assess_orientation_once(
        self, *, file, page_count: int
    ) -> bool:
        """Best-effort one-time assessment; never withhold the primary artifact."""

        if (
            self._orientation_detector is None
            or self._orientation_assessments is None
        ):
            return False
        claim = None
        error_code = "orientation_detection_failed"
        try:
            claim = await self._orientation_assessments.begin(
                file_id=file.file_id,
                batch_id=file.batch_id,
                result_version=2,
                page_count=page_count,
                now=self._now_factory(),
                lease_seconds=self._orientation_assessment_lease_seconds,
            )
            if claim is None:
                return False
            try:
                async with asyncio.timeout(
                    self._orientation_assessment_timeout_seconds
                ):
                    decisions = await self._orientation_detector.detect(
                        OrientationDetectionRequest(
                            batch_id=file.batch_id,
                            file_id=file.file_id,
                            page_count=page_count,
                            pages=tuple(range(1, page_count + 1)),
                        )
                    )
            except TimeoutError:
                error_code = "orientation_detection_timeout"
                raise
            suspected_pages = tuple(
                decision.page_number
                for decision in decisions
                if decision.credible and int(decision.angle) != 0
            )
            await self._orientation_assessments.complete(
                claim,
                suspected_pages=suspected_pages,
                now=self._now_factory(),
            )
            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            if claim is not None:
                try:
                    await self._orientation_assessments.fail(
                        claim,
                        error_code=error_code,
                        now=self._now_factory(),
                    )
                except Exception:
                    pass
            return True


__all__ = ["ProductionFilePipeline"]
