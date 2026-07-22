"""SQLAlchemy models for durable task metadata."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class BatchRecord(Base):
    __tablename__ = "batches"
    __table_args__ = (CheckConstraint("length(idempotency_key) > 0"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    total_files: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancelled_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processing_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queued_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_file_id: Mapped[str | None] = mapped_column(String(512))
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class FileTaskRecord(Base):
    __tablename__ = "file_tasks"
    __table_args__ = (
        UniqueConstraint("batch_id", "position"),
        CheckConstraint("progress >= 0 AND progress <= 100"),
        CheckConstraint("attempt_count >= 0"),
        CheckConstraint("max_attempts >= 1"),
    )

    id: Mapped[str] = mapped_column(String(512), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("batches.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lease_owner: Mapped[str | None] = mapped_column(String(512))
    lease_token: Mapped[str | None] = mapped_column(String(36), unique=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    completed_units: Mapped[int | None] = mapped_column(Integer)
    total_units: Mapped[int | None] = mapped_column(Integer)
    progress_unit: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class StageEventRecord(Base):
    __tablename__ = "stage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(
        String(512), ForeignKey("file_tasks.id", ondelete="CASCADE"), nullable=False
    )
    old_status: Mapped[str] = mapped_column(String(32), nullable=False)
    new_status: Mapped[str] = mapped_column(String(32), nullable=False)
    old_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    new_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    old_progress: Mapped[int] = mapped_column(Integer, nullable=False)
    new_progress: Mapped[int] = mapped_column(Integer, nullable=False)
    old_completed_units: Mapped[int | None] = mapped_column(Integer)
    new_completed_units: Mapped[int | None] = mapped_column(Integer)
    old_total_units: Mapped[int | None] = mapped_column(Integer)
    new_total_units: Mapped[int | None] = mapped_column(Integer)
    old_progress_unit: Mapped[str | None] = mapped_column(String(16))
    new_progress_unit: Mapped[str | None] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ArtifactRecord(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("file_id", "result_version"),
        UniqueConstraint("storage_key"),
        CheckConstraint("source_version >= 1"),
        CheckConstraint("result_version >= 1"),
        CheckConstraint("size_bytes >= 0"),
        CheckConstraint("audit_record_count >= 0"),
        CheckConstraint("version >= 1"),
    )

    id: Mapped[str] = mapped_column(String(73), primary_key=True)
    file_id: Mapped[str] = mapped_column(
        String(512), ForeignKey("file_tasks.id", ondelete="CASCADE"), nullable=False
    )
    batch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("batches.id", ondelete="CASCADE"), nullable=False
    )
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    result_version: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    audit_metadata_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    audit_record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ReplacementAuditMetadataRecord(Base):
    __tablename__ = "replacement_audit_metadata"
    __table_args__ = (
        CheckConstraint("source_version >= 1"),
        CheckConstraint("output_version >= 1"),
        CheckConstraint("confidence >= 0 AND confidence <= 1"),
    )

    audit_id: Mapped[str] = mapped_column(String(71), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(
        String(73), ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    file_id: Mapped[str] = mapped_column(
        String(512), ForeignKey("file_tasks.id", ondelete="CASCADE"), nullable=False
    )
    batch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("batches.id", ondelete="CASCADE"), nullable=False
    )
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    output_version: Mapped[int] = mapped_column(Integer, nullable=False)
    image_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    angle: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    engine: Mapped[str] = mapped_column(String(64), nullable=False)
    model_versions_json: Mapped[str] = mapped_column(String(4096), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
