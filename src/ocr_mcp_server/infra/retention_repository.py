"""Bounded SQLite claims for two-phase batch retention."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.exc import SQLAlchemyError

from ..domain.errors import RetentionErrorCode, RetentionFailure
from ..domain.retention import (
    ContentWriteGuard,
    RetentionClaim,
    RetentionPhase,
    RetentionSnapshot,
)
from .database import SessionFactory
from .task_models import (
    ArtifactRecord,
    BatchLockMarkerRecord,
    BatchRecord,
    FileTaskRecord,
    ReplacementAuditMetadataRecord,
    RetentionRecord,
    StageEventRecord,
)


_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None
    return value.astimezone(UTC)


def _db_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _valid_batch_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


class RetentionRepository:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def get(self, batch_id: str) -> RetentionSnapshot | None:
        try:
            async with self._sessions() as session:
                record = await session.get(RetentionRecord, batch_id)
                return None if record is None else self._snapshot(record)
        except SQLAlchemyError:
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None

    async def bind_lock_marker(
        self,
        batch_id: str,
        identity: tuple[int, int],
        *,
        created: bool,
        recover_unbound: bool,
        allow_missing: bool,
    ) -> None:
        if (
            not _valid_batch_id(batch_id)
            or not isinstance(identity, tuple)
            or len(identity) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in identity
            )
            or not isinstance(created, bool)
            or not isinstance(recover_unbound, bool)
            or not isinstance(allow_missing, bool)
        ):
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None
        encoded = f"{identity[0]:x}:{identity[1]:x}"
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                retention = await session.get(RetentionRecord, batch_id)
                if retention is None and not allow_missing:
                    raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT)
                marker = await session.get(BatchLockMarkerRecord, batch_id)
                if marker is None:
                    if not (created or recover_unbound):
                        raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
                    session.add(
                        BatchLockMarkerRecord(batch_id=batch_id, identity=encoded)
                    )
                elif marker.identity != encoded:
                    raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
                await session.commit()
        except RetentionFailure:
            raise
        except SQLAlchemyError:
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None

    async def acquire_content_write(
        self,
        batch_id: str,
        writer_id: str,
        *,
        now: datetime,
        lease_seconds: int,
        allow_missing: bool = False,
    ) -> ContentWriteGuard | None:
        now = _utc(now)
        if (
            not _valid_batch_id(batch_id)
            or not isinstance(writer_id, str)
            or not _OWNER.fullmatch(writer_id)
            or isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
            or not isinstance(allow_missing, bool)
        ):
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await session.get(RetentionRecord, batch_id)
                if record is None:
                    await session.commit()
                    if allow_missing:
                        return None
                    raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT)
                if (
                    record.early_delete
                    or record.content_deleted_at is not None
                    or record.claim_token is not None
                    or (
                        record.content_write_token is not None
                        and _db_utc(record.content_write_expires_at) > now
                    )
                ):
                    raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT)
                token = str(uuid4())
                expires_at = now + timedelta(seconds=lease_seconds)
                record.content_write_token = token
                record.content_write_file_id = writer_id
                record.content_write_expires_at = expires_at
                record.updated_at = now
                record.version += 1
                await session.commit()
                return ContentWriteGuard(
                    batch_id=batch_id,
                    file_task_id=writer_id,
                    token=token,
                    expires_at=expires_at,
                )
        except RetentionFailure:
            raise
        except SQLAlchemyError:
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None

    async def release_content_write(self, guard: ContentWriteGuard) -> None:
        if not isinstance(guard, ContentWriteGuard):
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await session.get(RetentionRecord, guard.batch_id)
                if record is not None and record.content_write_token == guard.token:
                    record.content_write_token = None
                    record.content_write_file_id = None
                    record.content_write_expires_at = None
                    record.version += 1
                await session.commit()
        except SQLAlchemyError:
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None

    async def request_early_delete(self, batch_id: str, *, now: datetime) -> bool:
        now = _utc(now)
        if not _valid_batch_id(batch_id):
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await session.get(RetentionRecord, batch_id)
                if record is None:
                    await session.commit()
                    return False
                if (
                    record.content_write_token is not None
                    and _db_utc(record.content_write_expires_at) <= now
                ):
                    record.content_write_token = None
                    record.content_write_file_id = None
                    record.content_write_expires_at = None
                record.early_delete = True
                record.content_due_at = min(_db_utc(record.content_due_at), now)
                record.metadata_due_at = min(_db_utc(record.metadata_due_at), now)
                record.updated_at = now
                record.version += 1
                await session.commit()
                return True
        except RetentionFailure:
            raise
        except SQLAlchemyError:
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None

    async def claim_due(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: int,
        limit: int,
    ) -> tuple[RetentionClaim, ...]:
        now = _utc(now)
        if (
            not isinstance(worker_id, str)
            or _OWNER.fullmatch(worker_id) is None
            or isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > 1000
        ):
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                available = or_(
                    RetentionRecord.claim_token.is_(None),
                    RetentionRecord.lease_expires_at <= now,
                )
                records = list((await session.scalars(
                    select(RetentionRecord)
                    .where(
                        RetentionRecord.content_deleted_at.is_(None),
                        RetentionRecord.content_due_at <= now,
                        available,
                    )
                    .order_by(RetentionRecord.content_due_at, RetentionRecord.batch_id)
                    .limit(limit)
                )).all())
                if len(records) < limit:
                    records.extend((await session.scalars(
                        select(RetentionRecord)
                        .where(
                            RetentionRecord.content_deleted_at.is_not(None),
                            RetentionRecord.metadata_due_at <= now,
                            available,
                        )
                        .order_by(RetentionRecord.metadata_due_at, RetentionRecord.batch_id)
                        .limit(limit - len(records))
                    )).all())
                claims = tuple(self._claim(record, worker_id, now, lease_seconds) for record in records)
                await session.commit()
                return claims
        except RetentionFailure:
            raise
        except SQLAlchemyError:
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None

    async def claim_batch(
        self,
        batch_id: str,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> RetentionClaim | None:
        now = _utc(now)
        if (
            not _valid_batch_id(batch_id)
            or not isinstance(worker_id, str)
            or _OWNER.fullmatch(worker_id) is None
            or isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
        ):
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await session.get(RetentionRecord, batch_id)
                if record is None:
                    await session.commit()
                    return None
                if record.claim_token is not None and _db_utc(record.lease_expires_at) > now:
                    raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT)
                due = (
                    _db_utc(record.content_due_at)
                    if record.content_deleted_at is None
                    else _db_utc(record.metadata_due_at)
                )
                if due > now:
                    await session.commit()
                    return None
                claim = self._claim(record, worker_id, now, lease_seconds)
                await session.commit()
                return claim
        except RetentionFailure:
            raise
        except SQLAlchemyError:
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None

    async def complete_content(self, claim: RetentionClaim, *, now: datetime) -> None:
        now = _utc(now)
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await self._require_claim(session, claim, RetentionPhase.CONTENT, now)
                await session.execute(
                    update(ArtifactRecord)
                    .where(ArtifactRecord.batch_id == claim.batch_id, ArtifactRecord.available.is_(True))
                    .values(available=False, deleted_at=now, version=ArtifactRecord.version + 1)
                )
                record.content_deleted_at = now
                self._clear_claim(record, now)
                await session.commit()
        except RetentionFailure:
            raise
        except SQLAlchemyError:
            raise RetentionFailure(RetentionErrorCode.CLEANUP_FAILED) from None

    async def prepare_tombstone(
        self,
        claim: RetentionClaim,
        *,
        root_kind: str,
        now: datetime,
    ) -> str:
        now = _utc(now)
        field = {
            "data": "data_tombstone",
            "artifact": "artifact_tombstone",
        }.get(root_kind)
        if field is None:
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await self._require_claim(
                    session, claim, RetentionPhase.CONTENT, now
                )
                name = getattr(record, field)
                if name is None:
                    name = f".retention-{uuid4()}"
                    setattr(record, field, name)
                    record.updated_at = now
                    record.version += 1
                await session.commit()
                return name
        except RetentionFailure:
            raise
        except SQLAlchemyError:
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None

    async def require_content_write_quiescent(
        self, claim: RetentionClaim, *, now: datetime
    ) -> None:
        now = _utc(now)
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await self._require_claim(
                    session, claim, RetentionPhase.CONTENT, now
                )
                if (
                    record.content_write_token is not None
                    and _db_utc(record.content_write_expires_at) > now
                ):
                    raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT)
                record.content_write_token = None
                record.content_write_file_id = None
                record.content_write_expires_at = None
                await session.commit()
        except RetentionFailure:
            raise
        except SQLAlchemyError:
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None

    async def fail_claim(self, claim: RetentionClaim, *, now: datetime, error_code: str) -> None:
        now = _utc(now)
        safe_code = error_code if re.fullmatch(r"[a-z0-9_.-]{1,128}", error_code or "") else "cleanup_failed"
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await session.get(RetentionRecord, claim.batch_id)
                if record is not None and record.claim_token == claim.claim_token:
                    record.last_error_code = safe_code
                    self._clear_claim(record, now)
                await session.commit()
        except SQLAlchemyError:
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None

    async def purge_metadata(self, claim: RetentionClaim, *, now: datetime) -> None:
        now = _utc(now)
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                await self._require_claim(session, claim, RetentionPhase.METADATA, now)
                file_ids = select(FileTaskRecord.id).where(FileTaskRecord.batch_id == claim.batch_id)
                await session.execute(delete(ReplacementAuditMetadataRecord).where(
                    ReplacementAuditMetadataRecord.batch_id == claim.batch_id
                ))
                await session.execute(delete(ArtifactRecord).where(ArtifactRecord.batch_id == claim.batch_id))
                await session.execute(delete(StageEventRecord).where(StageEventRecord.file_id.in_(file_ids)))
                await session.execute(delete(FileTaskRecord).where(FileTaskRecord.batch_id == claim.batch_id))
                await session.execute(
                    delete(BatchLockMarkerRecord).where(
                        BatchLockMarkerRecord.batch_id == claim.batch_id
                    )
                )
                await session.execute(delete(RetentionRecord).where(RetentionRecord.batch_id == claim.batch_id))
                await session.execute(delete(BatchRecord).where(BatchRecord.id == claim.batch_id))
                await session.commit()
        except RetentionFailure:
            raise
        except SQLAlchemyError:
            raise RetentionFailure(RetentionErrorCode.METADATA_PURGE_FAILED) from None

    @staticmethod
    def _claim(record: RetentionRecord, worker_id: str, now: datetime, lease_seconds: int) -> RetentionClaim:
        phase = RetentionPhase.CONTENT if record.content_deleted_at is None else RetentionPhase.METADATA
        token = str(uuid4())
        record.claim_phase = phase.value
        record.claim_token = token
        record.claim_owner = worker_id
        record.lease_expires_at = now + timedelta(seconds=lease_seconds)
        record.attempt_count += 1
        record.updated_at = now
        record.version += 1
        return RetentionClaim(
            batch_id=record.batch_id,
            phase=phase,
            claim_token=token,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            attempt=record.attempt_count,
        )

    @staticmethod
    async def _require_claim(session, claim: RetentionClaim, phase: RetentionPhase, now: datetime) -> RetentionRecord:
        if not isinstance(claim, RetentionClaim) or claim.phase is not phase:
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None
        record = await session.get(RetentionRecord, claim.batch_id)
        lease_expires_at = None if record is None else _db_utc(record.lease_expires_at)
        if (
            record is None
            or record.claim_token != claim.claim_token
            or record.claim_phase != phase.value
            or lease_expires_at is None
            or lease_expires_at <= now
        ):
            raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT) from None
        return record

    @staticmethod
    def _clear_claim(record: RetentionRecord, now: datetime) -> None:
        record.claim_phase = None
        record.claim_token = None
        record.claim_owner = None
        record.lease_expires_at = None
        record.updated_at = now
        record.version += 1

    @staticmethod
    def _snapshot(record: RetentionRecord) -> RetentionSnapshot:
        return RetentionSnapshot(
            batch_id=record.batch_id,
            content_due_at=_db_utc(record.content_due_at),
            metadata_due_at=_db_utc(record.metadata_due_at),
            content_deleted_at=_db_utc(record.content_deleted_at),
            early_delete=record.early_delete,
            version=record.version,
        )
