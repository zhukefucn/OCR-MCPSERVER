"""Durable content-free mapping from public upload IDs to stored inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, exists, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ..domain.errors import PersistenceError
from ..domain.files import StoredFile, SupportedMediaType
from .database import SessionFactory
from .task_models import (
    FileTaskRecord,
    OrientationRecoveryRecord,
    UploadRecord,
)


@dataclass(frozen=True, slots=True)
class UploadSnapshot:
    file_id: str
    storage_batch_id: str
    sha256: str
    size_bytes: int
    media_type: SupportedMediaType
    extension: str
    page_count: int
    width: int | None
    height: int | None
    created_at: datetime
    expires_at: datetime
    adopted_source_file_id: str | None
    result_version: int | None


class UploadRepository:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def register(
        self,
        stored: StoredFile,
        *,
        storage_batch_id: str,
        idempotency_key: str | None,
        created_at: datetime,
        expires_at: datetime | None = None,
        adopted_source_file_id: str | None = None,
        result_version: int | None = None,
    ) -> UploadSnapshot:
        if (
            not isinstance(stored, StoredFile)
            or not isinstance(created_at, datetime)
            or created_at.tzinfo is None
            or (idempotency_key is not None and not idempotency_key)
        ):
            raise ValueError("invalid upload metadata")
        _uuid(stored.file_id)
        _uuid(storage_batch_id)
        expires_at = expires_at or (created_at + timedelta(hours=24))
        if expires_at.tzinfo is None or expires_at <= created_at:
            raise ValueError("invalid upload expiry")
        if (adopted_source_file_id is None) != (result_version is None):
            raise ValueError("incomplete upload adoption metadata")
        if adopted_source_file_id is not None:
            _uuid(adopted_source_file_id)
            if type(result_version) is not int or result_version < 1:
                raise ValueError("invalid upload result version")
        record = UploadRecord(
            file_id=stored.file_id,
            storage_batch_id=storage_batch_id,
            idempotency_key=idempotency_key,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            media_type=stored.media_type.value,
            extension=stored.extension,
            page_count=stored.page_count,
            width=stored.width,
            height=stored.height,
            created_at=created_at.astimezone(UTC),
            expires_at=expires_at.astimezone(UTC),
            retired_at=None,
            adopted_source_file_id=adopted_source_file_id,
            result_version=result_version,
        )
        try:
            async with self._sessions() as session:
                session.add(record)
                await session.commit()
            return self._snapshot(record)
        except IntegrityError:
            if idempotency_key is not None:
                existing = await self.get_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return existing
            raise PersistenceError() from None
        except SQLAlchemyError as exc:
            raise PersistenceError(cause=exc) from None

    async def get(self, file_id: str) -> UploadSnapshot | None:
        _uuid(file_id)
        try:
            async with self._sessions() as session:
                record = await session.get(UploadRecord, file_id)
                if record is not None and record.retired_at is not None:
                    return None
                return None if record is None else self._snapshot(record)
        except SQLAlchemyError as exc:
            raise PersistenceError(cause=exc) from None

    async def get_by_idempotency_key(
        self, key: str
    ) -> UploadSnapshot | None:
        if not isinstance(key, str) or not key:
            raise ValueError("invalid idempotency key")
        try:
            async with self._sessions() as session:
                record = await session.scalar(
                    select(UploadRecord).where(
                        UploadRecord.idempotency_key == key,
                        UploadRecord.retired_at.is_(None),
                    )
                )
                return None if record is None else self._snapshot(record)
        except SQLAlchemyError as exc:
            raise PersistenceError(cause=exc) from None

    @staticmethod
    def _snapshot(record: UploadRecord) -> UploadSnapshot:
        created_at = record.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return UploadSnapshot(
            file_id=record.file_id,
            storage_batch_id=record.storage_batch_id,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
            media_type=SupportedMediaType(record.media_type),
            extension=record.extension,
            page_count=record.page_count,
            width=record.width,
            height=record.height,
            created_at=created_at.astimezone(UTC),
            expires_at=(
                record.expires_at.replace(tzinfo=UTC)
                if record.expires_at.tzinfo is None
                else record.expires_at.astimezone(UTC)
            ),
            adopted_source_file_id=record.adopted_source_file_id,
            result_version=record.result_version,
        )

    async def claim_expired(
        self, *, now: datetime, limit: int
    ) -> tuple[UploadSnapshot, ...]:
        if now.tzinfo is None or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid upload cleanup request")
        now_db = now.astimezone(UTC)
        active = exists(
            select(FileTaskRecord.id).where(
                FileTaskRecord.id == UploadRecord.file_id,
                FileTaskRecord.status.in_(("queued", "processing")),
            )
        )
        active_recovery = exists(
            select(OrientationRecoveryRecord.token_digest).where(
                OrientationRecoveryRecord.file_id == UploadRecord.file_id,
                OrientationRecoveryRecord.state == "claimed",
            )
        )
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                ids = tuple(
                    await session.scalars(
                        select(UploadRecord.file_id)
                        .where(
                            UploadRecord.expires_at <= now_db,
                            UploadRecord.retired_at.is_(None),
                            ~active,
                            ~active_recovery,
                        )
                        .order_by(UploadRecord.expires_at, UploadRecord.file_id)
                        .limit(limit)
                    )
                )
                claimed: list[UploadSnapshot] = []
                for file_id in ids:
                    result = await session.execute(
                        update(UploadRecord)
                        .where(
                            UploadRecord.file_id == file_id,
                            UploadRecord.retired_at.is_(None),
                            ~active,
                            ~active_recovery,
                        )
                        .values(retired_at=now_db)
                        .returning(UploadRecord)
                    )
                    record = result.scalar_one_or_none()
                    if record is not None:
                        claimed.append(self._snapshot(record))
                await session.commit()
                return tuple(claimed)
        except SQLAlchemyError as exc:
            raise PersistenceError(cause=exc) from None

    async def list_expired_candidates(
        self, *, now: datetime, limit: int
    ) -> tuple[UploadSnapshot, ...]:
        if (
            now.tzinfo is None
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise ValueError("invalid upload cleanup request")
        active = exists(
            select(FileTaskRecord.id).where(
                FileTaskRecord.id == UploadRecord.file_id,
                FileTaskRecord.status.in_(("queued", "processing")),
            )
        )
        active_recovery = exists(
            select(OrientationRecoveryRecord.token_digest).where(
                OrientationRecoveryRecord.file_id == UploadRecord.file_id,
                OrientationRecoveryRecord.state == "claimed",
            )
        )
        try:
            async with self._sessions() as session:
                records = tuple(
                    await session.scalars(
                        select(UploadRecord)
                        .where(
                            UploadRecord.expires_at <= now.astimezone(UTC),
                            UploadRecord.retired_at.is_(None),
                            ~active,
                            ~active_recovery,
                        )
                        .order_by(UploadRecord.expires_at, UploadRecord.file_id)
                        .limit(limit)
                    )
                )
                return tuple(self._snapshot(record) for record in records)
        except SQLAlchemyError as exc:
            raise PersistenceError(cause=exc) from None

    async def claim_expired_one(
        self, file_id: str, *, now: datetime
    ) -> UploadSnapshot | None:
        _uuid(file_id)
        if now.tzinfo is None:
            raise ValueError("invalid upload cleanup request")
        now_db = now.astimezone(UTC)
        active = exists(
            select(FileTaskRecord.id).where(
                FileTaskRecord.id == UploadRecord.file_id,
                FileTaskRecord.status.in_(("queued", "processing")),
            )
        )
        active_recovery = exists(
            select(OrientationRecoveryRecord.token_digest).where(
                OrientationRecoveryRecord.file_id == UploadRecord.file_id,
                OrientationRecoveryRecord.state == "claimed",
            )
        )
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                result = await session.execute(
                    update(UploadRecord)
                    .where(
                        UploadRecord.file_id == file_id,
                        UploadRecord.expires_at <= now_db,
                        UploadRecord.retired_at.is_(None),
                        ~active,
                        ~active_recovery,
                    )
                    .values(retired_at=now_db)
                    .returning(UploadRecord)
                )
                record = result.scalar_one_or_none()
                await session.commit()
                return None if record is None else self._snapshot(record)
        except SQLAlchemyError as exc:
            raise PersistenceError(cause=exc) from None

    async def delete_retired(self, file_id: str) -> bool:
        _uuid(file_id)
        try:
            async with self._sessions() as session:
                result = await session.execute(
                    delete(UploadRecord).where(
                        UploadRecord.file_id == file_id,
                        UploadRecord.retired_at.is_not(None),
                    )
                )
                await session.commit()
                return result.rowcount == 1
        except SQLAlchemyError as exc:
            raise PersistenceError(cause=exc) from None


def _uuid(value: object) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid upload identifier") from None


__all__ = ["UploadRepository", "UploadSnapshot"]
