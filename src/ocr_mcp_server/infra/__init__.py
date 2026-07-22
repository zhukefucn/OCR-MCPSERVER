"""Stable infrastructure interfaces for durable task metadata."""

from .database import (
    SessionFactory,
    create_database_engine,
    create_session_factory,
    initialize_schema,
)
from .task_repository import TaskRepository
from .artifact_repository import ArtifactRepository
from .mineru_adapter import MinerUAdapter, ProgressCallback
from .secondary_ocr import (
    SecondaryOcrWorkerLifecycle,
    SingleOwnerSecondaryOcrWorker,
    SynchronousSecondaryOcrBackend,
)

__all__ = [
    "ArtifactRepository",
    "SessionFactory",
    "MinerUAdapter",
    "ProgressCallback",
    "SecondaryOcrWorkerLifecycle",
    "SingleOwnerSecondaryOcrWorker",
    "SynchronousSecondaryOcrBackend",
    "TaskRepository",
    "create_database_engine",
    "create_session_factory",
    "initialize_schema",
]
