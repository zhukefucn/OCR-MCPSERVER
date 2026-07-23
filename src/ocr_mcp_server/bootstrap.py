"""Production resource ownership and process lifecycle composition."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import os
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .infra.database import SessionFactory, initialize_schema
from .infra.artifact_repository import ArtifactRepository
from .infra.health_probes import (
    MinerUReadinessProbe,
    PaddleReadinessProbe,
    SqliteReadinessProbe,
)
from .infra.mineru_adapter import MinerUAdapter
from .infra.orientation_repository import OrientationRecoveryRepository
from .infra.document_orientation import ImmutableDocumentCorrector
from .infra.pp_structure_v3 import create_pp_structure_v3_provider
from .infra.retention_repository import RetentionRepository
from .infra.task_repository import TaskRepository
from .infra.upload_repository import UploadRepository
from .services.artifacts import ArtifactBundler, ArtifactPackagingStep
from .services.artifact_download import ArtifactDownloadService
from .services.file_intake import FileIntakeService
from .services.file_storage import FileStorage
from .services.file_validation import FileValidator
from .services.health import ReadinessService
from .services.orchestration import OrchestrationService
from .services.production_pipeline import ProductionFilePipeline
from .services.remote_fetch import RemoteFileFetcher
from .services.retention import RetentionService
from .services.staging_retention import (
    ProductionRetentionWorker,
    StagingUploadRetention,
)
from .domain.models import utc_now
from .api.production_gateway import ProductionDocumentGateway
from .services.orientation_recovery import (
    OrientationRecoveryCoordinator,
    ValidatedPageOrientationDetector,
)
from .services.production_recovery import (
    MappedDocumentCorrector,
    MappedRecoveryStorage,
    ProductionPageOrientationDetector,
    RecoveryPipelineRunner,
)
from .settings import AppSettings


SchemaInitializer = Callable[[AsyncEngine], Awaitable[None]]


class RuntimeResources:
    """Own every process-scoped runtime resource and close it exactly once."""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        session_factory: SessionFactory,
        repositories: Mapping[str, object],
        mineru_adapter: object,
        mineru_client: httpx.AsyncClient,
        paddle_worker: object,
        orchestration: object,
        retention_service: object,
        orientation_recovery: object,
        document_gateway: object,
        readiness: ReadinessService,
        artifact_download: object | None = None,
        schema_initializer: SchemaInitializer = initialize_schema,
    ) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.repositories = dict(repositories)
        self.mineru_adapter = mineru_adapter
        self.mineru_client = mineru_client
        self.paddle_worker = paddle_worker
        self.orchestration = orchestration
        self.retention_service = retention_service
        self.orientation_recovery = orientation_recovery
        self.document_gateway = document_gateway
        self.readiness = readiness
        self.artifact_download = artifact_download
        self._schema_initializer = schema_initializer
        self._started = False
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._invoked: list[object] = []

    async def start(self) -> None:
        if self._started or self._closed:
            raise RuntimeError("runtime lifecycle is invalid")
        try:
            await self._schema_initializer(self.engine)
            for resource in (
                self.paddle_worker,
                self.orchestration,
                self.retention_service,
            ):
                if not callable(getattr(resource, "start", None)):
                    continue
                self._invoked.append(resource)
                await resource.start()
            self._started = True
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            for resource in reversed(self._invoked):
                await _best_effort_async_close(resource)
            _best_effort_close_observability(self.orientation_recovery)
            await _best_effort_async_close(self.mineru_client, method="aclose")
            await _best_effort_async_close(self.engine, method="dispose")


def build_runtime(
    settings: AppSettings,
    observability=None,
    event_logger=None,
) -> RuntimeResources:
    """Construct the production graph without starting threads or loading models."""

    del event_logger
    engine = __import_database_engine(settings)
    sessions = __import_session_factory(engine)
    tasks = TaskRepository(
        sessions,
        max_batch_size_bytes=settings.limits.max_batch_size_bytes,
    )
    uploads = UploadRepository(sessions)
    artifacts = ArtifactRepository(sessions)
    retention_repository = RetentionRepository(sessions)
    orientation_repository = OrientationRecoveryRepository(sessions)
    storage = FileStorage(settings.data_root)
    validator = FileValidator(
        max_pages=settings.limits.max_pages,
        max_image_pixels=settings.remote_import.max_image_pixels,
    )
    intake = FileIntakeService(
        storage=storage,
        content_write_guards=retention_repository,
        validator=validator,
        max_files=settings.limits.max_files,
        max_file_size_bytes=settings.limits.max_file_size_bytes,
        max_batch_size_bytes=settings.limits.max_batch_size_bytes,
        write_lease_seconds=settings.retention.cleanup_lease_seconds,
    )
    mineru_client = httpx.AsyncClient(follow_redirects=False)
    mineru = MinerUAdapter(client=mineru_client, settings=settings.mineru)
    paddle = create_pp_structure_v3_provider(settings.secondary_ocr)
    artifact_root = Path(settings.data_root).absolute() / "artifacts"
    packaging = ArtifactPackagingStep(
        ArtifactBundler(settings.artifacts.to_limits()),
        artifacts,
        batch_locks=storage,
        marker_registry=retention_repository,
        write_lease_seconds=settings.retention.cleanup_lease_seconds,
    )
    artifact_download = ArtifactDownloadService(artifacts, artifact_root)
    pipeline = ProductionFilePipeline(
        uploads=uploads,
        storage=storage,
        mineru=mineru,
        paddle=paddle,
        packaging=packaging,
        data_root=settings.data_root,
        artifact_root=artifact_root,
        max_file_size_bytes=settings.limits.max_file_size_bytes,
        max_image_pixels=settings.remote_import.max_image_pixels,
        structured_limits=settings.structured_content.to_limits(),
        result_retention_hours=settings.retention.result_hours,
    )
    orchestration = OrchestrationService(
        tasks,
        pipeline,
        settings.orchestration,
        worker_identity=f"ocr-gateway-{os.getpid()}",
        observability=observability,
    )
    batch_retention = RetentionService(
        retention_repository,
        settings.data_root,
        artifact_root,
    )
    retention_service = ProductionRetentionWorker(
        batch_retention=batch_retention,
        staging_retention=StagingUploadRetention(
            uploads,
            settings.data_root,
            storage=storage,
            marker_registry=retention_repository,
        ),
        now_factory=utc_now,
        interval_seconds=settings.retention.cleanup_interval_seconds,
        lease_seconds=settings.retention.cleanup_lease_seconds,
        batch_size=settings.retention.cleanup_batch_size,
        worker_id=f"ocr-retention-{os.getpid()}",
    )
    mapped_recovery_storage = MappedRecoveryStorage(uploads, tasks, storage)
    recovery_detector = ValidatedPageOrientationDetector(
        ProductionPageOrientationDetector(
            uploads,
            storage,
            paddle,
            max_file_size_bytes=settings.limits.max_file_size_bytes,
            max_image_pixels=settings.remote_import.max_image_pixels,
        ),
        credibility_threshold=settings.secondary_ocr.classification_threshold,
    )
    recovery_corrector = MappedDocumentCorrector(
        delegate=ImmutableDocumentCorrector(
            storage,
            max_file_size_bytes=settings.limits.max_file_size_bytes,
            max_batch_size_bytes=settings.limits.max_batch_size_bytes,
            max_pages=settings.limits.max_pages,
            max_image_pixels=settings.remote_import.max_image_pixels,
        ),
        uploads=uploads,
        retention_hours=settings.retention.input_hours,
    )
    recovery_runner = RecoveryPipelineRunner(
        intake=intake,
        uploads=uploads,
        tasks=tasks,
        orchestration=orchestration,
        retention_hours=settings.retention.input_hours,
        retention_options={
            "input_retention_hours": settings.retention.input_hours,
            "intermediate_retention_hours": settings.retention.intermediate_hours,
            "result_retention_hours": settings.retention.result_hours,
            "audit_metadata_retention_days": settings.retention.audit_metadata_days,
        },
    )
    recovery = OrientationRecoveryCoordinator(
        repository=orientation_repository,
        detector=recovery_detector,
        corrector=recovery_corrector,
        runner=recovery_runner,
        storage=mapped_recovery_storage,
        marker_registry=retention_repository,
        content_write_guards=retention_repository,
        write_lease_seconds=settings.retention.cleanup_lease_seconds,
        observability=observability,
    )
    remote_fetcher = RemoteFileFetcher(
        client=mineru_client,
        allowed_hosts=settings.remote_import.allowed_hosts,
        max_file_size_bytes=settings.limits.max_file_size_bytes,
        max_redirects=settings.remote_import.max_redirects,
        timeout_seconds=settings.remote_import.timeout_seconds,
    )
    gateway = ProductionDocumentGateway(
        intake=intake,
        uploads=uploads,
        tasks=tasks,
        orchestration=orchestration,
        artifacts=artifacts,
        recovery=recovery,
        orientation_issuer=orientation_repository,
        remote_fetcher=remote_fetcher,
        retention_options={
            "input_retention_hours": settings.retention.input_hours,
            "intermediate_retention_hours": settings.retention.intermediate_hours,
            "result_retention_hours": settings.retention.result_hours,
            "audit_metadata_retention_days": settings.retention.audit_metadata_days,
        },
        artifact_base_url=f"{str(settings.public_base_url).rstrip('/')}/v1/artifacts",
    )
    readiness = build_readiness(
        session_factory=sessions,
        mineru_client=mineru_client,
        mineru_api_url=str(settings.mineru.api_url),
        paddle_worker=paddle,
        timeout_seconds=settings.health.probe_timeout_seconds,
        observability=observability,
    )
    return RuntimeResources(
        engine=engine,
        session_factory=sessions,
        repositories={
            "tasks": tasks,
            "uploads": uploads,
            "artifacts": artifacts,
            "retention": retention_repository,
            "orientation": orientation_repository,
        },
        mineru_adapter=mineru,
        mineru_client=mineru_client,
        paddle_worker=paddle,
        orchestration=orchestration,
        retention_service=retention_service,
        orientation_recovery=recovery,
        document_gateway=gateway,
        readiness=readiness,
        artifact_download=artifact_download,
    )


async def sqlite_health(session_factory: SessionFactory) -> bool:
    """Perform the readiness-only passive SQLite query."""

    async with session_factory() as session:
        return await session.scalar(text("SELECT 1")) == 1


async def mineru_health(client: httpx.AsyncClient, api_url: str) -> bool:
    """Call only the fixed MinerU health endpoint."""

    url = f"{api_url.rstrip('/')}/health"
    response = await client.get(url)
    return response.status_code == 200


def build_readiness(
    *,
    session_factory: SessionFactory,
    mineru_client: httpx.AsyncClient,
    mineru_api_url: str,
    paddle_worker: object,
    timeout_seconds: float,
    observability=None,
) -> ReadinessService:
    """Build the strict three-dependency readiness service."""

    return ReadinessService(
        (
            SqliteReadinessProbe(lambda: sqlite_health(session_factory)),
            MinerUReadinessProbe(
                lambda: mineru_health(mineru_client, mineru_api_url)
            ),
            PaddleReadinessProbe(paddle_worker),
        ),
        timeout_seconds,
        observability,
    )


async def _best_effort_async_close(resource: Any, *, method: str = "close") -> None:
    try:
        close = getattr(resource, method)
        result = close()
        if isinstance(result, Awaitable):
            await result
    except BaseException:
        pass


def _best_effort_close_observability(resource: Any) -> None:
    try:
        resource.close_observability()
    except BaseException:
        pass


def __import_database_engine(settings: AppSettings) -> AsyncEngine:
    from .infra.database import create_database_engine

    return create_database_engine(
        settings.database.url,
        busy_timeout_ms=settings.database.busy_timeout_ms,
    )


def __import_session_factory(engine: AsyncEngine) -> SessionFactory:
    from .infra.database import create_session_factory

    return create_session_factory(engine)


__all__ = [
    "RuntimeResources",
    "build_runtime",
    "build_readiness",
    "mineru_health",
    "sqlite_health",
]
