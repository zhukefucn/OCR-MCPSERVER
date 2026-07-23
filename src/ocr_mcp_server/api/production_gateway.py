"""Concrete transport-neutral facade over the durable OCR services."""

from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

from ..domain.files import IncomingFile
from ..domain.models import BatchStatus, utc_now
from ..domain.orientation import OrientationFailure, RecoveryTokenBinding
from ..services.orientation_recovery import (
    OrientationRecoveryCommand,
    RecoveryServiceErrorCode,
    RecoveryServiceFailure,
)
from ..services.artifact_download import public_artifact_id
from .contracts import (
    ArtifactReference,
    BatchStatusResponse,
    FileStatusResponse,
    OrientationReparseRequest,
    OrientationReparseSubmission,
    ParseDocumentsRequest,
    ParseSubmission,
    SafeError,
    UploadReceipt,
)
from .gateway import (
    GatewayCapacityExceeded,
    GatewayConflict,
    GatewayFailure,
    GatewayInvalidRequest,
    GatewayNotFound,
    GatewayOrientationUncertain,
    GatewayUnavailable,
    ProgressCallback,
)


class ProductionDocumentGateway:
    """Keep REST and MCP on one four-method durable application boundary."""

    def __init__(
        self,
        *,
        intake,
        uploads,
        tasks,
        orchestration,
        artifacts,
        recovery,
        orientation_issuer=None,
        remote_fetcher=None,
        retention_options: dict[str, int] | None = None,
        upload_retention_hours: int = 24,
        artifact_base_url: str = "https://localhost/v1/artifacts",
        now_factory=utc_now,
        id_factory=lambda: str(uuid4()),
    ) -> None:
        self._intake = intake
        self._uploads = uploads
        self._tasks = tasks
        self._orchestration = orchestration
        self._artifacts = artifacts
        self._recovery = recovery
        self._orientation_issuer = orientation_issuer
        self._issued_tokens: dict[tuple[str, int], str] = {}
        self._remote_fetcher = remote_fetcher
        self._retention_options = dict(retention_options or {})
        self._upload_retention_hours = upload_retention_hours
        self._artifact_base_url = artifact_base_url.rstrip("/")
        self._now_factory = now_factory
        self._id_factory = id_factory

    async def upload_document(
        self,
        content,
        *,
        display_name: str,
        media_type: str,
        content_length: int | None,
        idempotency_key: str | None,
    ) -> UploadReceipt:
        del content_length
        try:
            if idempotency_key is not None:
                existing = await self._uploads.get_by_idempotency_key(
                    idempotency_key
                )
                if existing is not None:
                    return _upload_receipt(existing)
            storage_batch_id = self._id_factory()
            stored = await self._intake.ingest_upload(
                storage_batch_id,
                IncomingFile(display_name, media_type, content),
            )
            created_at = self._now_factory()
            snapshot = await self._uploads.register(
                stored,
                storage_batch_id=storage_batch_id,
                idempotency_key=idempotency_key,
                created_at=created_at,
                expires_at=created_at
                + timedelta(hours=self._upload_retention_hours),
            )
            if snapshot.file_id != stored.file_id:
                discard = getattr(self._intake, "discard_upload", None)
                if callable(discard):
                    await discard(storage_batch_id)
            return _upload_receipt(snapshot)
        except GatewayFailure:
            raise
        except ValueError:
            raise GatewayInvalidRequest() from None
        except Exception as exc:
            raise _mapped_failure(exc) from None

    async def parse_documents(
        self,
        request: ParseDocumentsRequest,
        *,
        progress: ProgressCallback | None = None,
    ) -> ParseSubmission:
        try:
            fingerprint = self.source_fingerprint(request.sources)
            if request.idempotency_key is not None:
                lookup = getattr(self._tasks, "get_by_idempotency_key", None)
                if callable(lookup):
                    existing = await lookup(request.idempotency_key)
                    if existing is not None:
                        if getattr(existing, "source_fingerprint", None) != fingerprint:
                            raise GatewayConflict()
                        return ParseSubmission(
                            batch_id=existing.batch.id, status=existing.batch.status
                        )
            file_ids: list[str] = []
            total = len(request.sources)
            for index, source in enumerate(request.sources, start=1):
                if source.file_id is not None:
                    upload = await self._uploads.get(source.file_id)
                    if upload is None:
                        raise GatewayNotFound()
                else:
                    if self._remote_fetcher is None:
                        raise GatewayUnavailable()
                    storage_batch_id = self._id_factory()
                    stored = await self._intake.ingest_remote(
                        storage_batch_id,
                        str(source.url),
                        self._remote_fetcher,
                    )
                    created_at = self._now_factory()
                    upload = await self._uploads.register(
                        stored,
                        storage_batch_id=storage_batch_id,
                        idempotency_key=None,
                        created_at=created_at,
                        expires_at=created_at
                        + timedelta(hours=self._upload_retention_hours),
                    )
                if upload.file_id in file_ids:
                    raise GatewayInvalidRequest()
                file_ids.append(upload.file_id)
                await _progress(progress, index, total)
            key = request.idempotency_key or f"parse:{self._id_factory()}"
            adoption_at = self._now_factory()
            created = await self._tasks.create_batch(
                key,
                file_ids,
                source_fingerprint=fingerprint,
                require_available_uploads_at=adoption_at,
                **self._retention_options,
            )
            if created.created:
                self._orchestration.notify_work()
            return ParseSubmission(
                batch_id=created.batch.id, status=created.batch.status
            )
        except GatewayFailure:
            raise
        except ValueError:
            raise GatewayInvalidRequest() from None
        except Exception as exc:
            raise _mapped_failure(exc) from None

    async def get_task_status(self, batch_id: str) -> BatchStatusResponse:
        try:
            batch = await self._tasks.get_batch(batch_id)
            if batch is None:
                raise GatewayNotFound()
            files = await self._tasks.list_batch_files(batch_id)
            artifacts = await self._artifacts.list_for_batch(batch_id)
            terminal = batch.status in {
                BatchStatus.COMPLETED,
                BatchStatus.COMPLETED_WITH_ERRORS,
                BatchStatus.FAILED,
                BatchStatus.CANCELLED,
            }
            references = (
                [
                    ArtifactReference(
                        artifact_id=public_artifact_id(item.artifact_id),
                        download_url=(
                            f"{self._artifact_base_url}/{item.artifact_id}"
                        ),
                        expires_at=item.expires_at,
                    )
                    for item in artifacts
                    if item.available
                ]
                if terminal
                else []
            )
            recovery_tokens = await self._recovery_tokens(
                batch_id=batch_id, files=files, artifacts=artifacts
            )
            return BatchStatusResponse(
                batch_id=batch.id,
                status=batch.status,
                progress=batch.progress,
                total_files=batch.total_files,
                completed_files=batch.completed_files,
                failed_files=batch.failed_files,
                files=[
                    FileStatusResponse(
                        file_id=item.id,
                        status=item.status,
                        stage=item.stage,
                        progress=item.progress,
                        error=(
                            None
                            if item.last_error_code is None
                            else SafeError(
                                code=_safe_error_code(item.last_error_code),
                                message="The file could not be processed.",
                            )
                        ),
                        recovery_token=recovery_tokens.get(item.id),
                    )
                    for item in files
                ],
                artifacts=references,
            )
        except GatewayFailure:
            raise
        except ValueError:
            raise GatewayInvalidRequest() from None
        except Exception as exc:
            raise _mapped_failure(exc) from None

    @staticmethod
    def source_fingerprint(sources) -> str:
        canonical = "\n".join(
            f"file:{source.file_id}"
            if source.file_id is not None
            else f"url:{str(source.url)}"
            for source in sources
        )
        return sha256(("parse-sources\0" + canonical).encode()).hexdigest()

    async def _recovery_tokens(self, *, batch_id, files, artifacts) -> dict[str, str]:
        if self._orientation_issuer is None:
            return {}
        by_file = {
            item.file_id: item for item in artifacts
            if item.available and hasattr(item, "file_id")
        }
        decorated: dict[str, str] = {}
        for file in files:
            artifact = by_file.get(file.id)
            if artifact is None:
                continue
            key = (file.id, artifact.result_version)
            if key in self._issued_tokens:
                decorated[file.id] = self._issued_tokens[key]
                continue
            upload = await self._uploads.get(file.id)
            if upload is None:
                continue
            try:
                issued = await self._orientation_issuer.issue(
                    RecoveryTokenBinding(
                        file_id=file.id,
                        batch_id=batch_id,
                        source_result_version=artifact.result_version,
                        page_count=upload.page_count,
                        suspected_pages=tuple(range(1, upload.page_count + 1)),
                        expires_at=artifact.expires_at,
                    ),
                    now=self._now_factory(),
                )
            except OrientationFailure:
                continue
            self._issued_tokens[key] = issued.token
            decorated[file.id] = issued.token
        return decorated

    async def reparse_with_page_orientation(
        self,
        request: OrientationReparseRequest,
        *,
        progress: ProgressCallback | None = None,
    ) -> OrientationReparseSubmission:
        try:
            result = await self._recovery.reparse(
                OrientationRecoveryCommand(
                    recovery_token=request.recovery_token,
                    pages=None if request.pages is None else tuple(request.pages),
                ),
                progress=progress,
            )
            return OrientationReparseSubmission(
                batch_id=result.batch_id, status=result.status
            )
        except RecoveryServiceFailure as exc:
            raise {
                RecoveryServiceErrorCode.TOKEN_INVALID: GatewayNotFound,
                RecoveryServiceErrorCode.REQUEST_INVALID: GatewayInvalidRequest,
                RecoveryServiceErrorCode.CONFLICT: GatewayConflict,
                RecoveryServiceErrorCode.UNCERTAIN: GatewayOrientationUncertain,
                RecoveryServiceErrorCode.UNAVAILABLE: GatewayUnavailable,
                RecoveryServiceErrorCode.PROCESSING_FAILED: GatewayFailure,
            }[exc.code]() from None
        except GatewayFailure:
            raise
        except ValueError:
            raise GatewayInvalidRequest() from None
        except Exception as exc:
            raise _mapped_failure(exc) from None


def _upload_receipt(value) -> UploadReceipt:
    media_type = getattr(value.media_type, "value", value.media_type)
    return UploadReceipt(
        file_id=value.file_id,
        size_bytes=value.size_bytes,
        media_type=media_type,
    )


async def _progress(
    callback: ProgressCallback | None, completed: int, total: int
) -> None:
    if callback is None:
        return
    try:
        await callback(completed, total)
    except Exception:
        pass


def _safe_error_code(value: object) -> str:
    if isinstance(value, str):
        normalized = value.replace("-", "_")
        if normalized and normalized[0].isalpha() and all(
            character.islower()
            or character.isdigit()
            or character == "_"
            for character in normalized
        ):
            return normalized[:64]
    return "processing_failed"


def _mapped_failure(exc: BaseException) -> GatewayFailure:
    code = getattr(exc, "code", "")
    if "capacity" in code or "too_large" in code:
        return GatewayCapacityExceeded()
    if "invalid" in code or "unsupported" in code:
        return GatewayInvalidRequest()
    if "conflict" in code:
        return GatewayConflict()
    return GatewayUnavailable()


__all__ = ["ProductionDocumentGateway"]
