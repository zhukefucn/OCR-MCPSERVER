"""Atomic SQLite state for opaque whole-page orientation recovery tokens."""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ..domain.orientation import (
    OrientationErrorCode,
    OrientationFailure,
    RecoveryClaim,
    RecoverySnapshot,
    RecoveryState,
    RecoveryTokenBinding,
    RecoveryTokenIssue,
    canonical_pages,
)
from .database import SessionFactory
from .task_models import (
    ArtifactRecord,
    BatchRecord,
    FileTaskRecord,
    OrientationRecoveryRecord,
    RetentionRecord,
)


_ERROR_CODE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
_MAX_LIFETIME = timedelta(hours=24)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OrientationFailure(OrientationErrorCode.REQUEST_INVALID) from None
    return value.astimezone(UTC)


def _db_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _digest(token: object) -> str:
    if not isinstance(token, str) or not 32 <= len(token) <= 256:
        raise OrientationFailure(OrientationErrorCode.TOKEN_INVALID) from None
    return sha256(token.encode("utf-8")).hexdigest()


def _encode_pages(pages: tuple[int, ...]) -> str:
    return ",".join(str(page) for page in pages)


def _decode_pages(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(page) for page in value.split(","))
    except (TypeError, ValueError):
        raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None


def _fingerprint(pages: tuple[int, ...]) -> str:
    return sha256(("orientation-pages\0" + _encode_pages(pages)).encode()).hexdigest()


def _valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False


class OrientationRecoveryRepository:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def issue(
        self, binding: RecoveryTokenBinding, *, now: datetime
    ) -> RecoveryTokenIssue:
        now = _utc(now)
        if (
            not isinstance(binding, RecoveryTokenBinding)
            or binding.expires_at <= now
            or binding.expires_at > now + _MAX_LIFETIME
        ):
            raise OrientationFailure(OrientationErrorCode.TOKEN_INVALID) from None
        for _ in range(3):
            token = "or_" + secrets.token_hex(32)
            record = OrientationRecoveryRecord(
                token_digest=_digest(token),
                file_id=binding.file_id,
                batch_id=binding.batch_id,
                source_result_version=binding.source_result_version,
                page_count=binding.page_count,
                suspected_pages=_encode_pages(binding.suspected_pages),
                selected_pages=None,
                expires_at=binding.expires_at,
                state=RecoveryState.ISSUED.value,
                request_fingerprint=None,
                claim_id=None,
                corrected_input_version=None,
                result_batch_id=None,
                result_version=None,
                error_code=None,
                created_at=now,
                updated_at=now,
                version=1,
            )
            try:
                async with self._sessions() as session:
                    await session.execute(text("BEGIN IMMEDIATE"))
                    file_record = await session.get(FileTaskRecord, binding.file_id)
                    if file_record is None or file_record.batch_id != binding.batch_id:
                        raise OrientationFailure(OrientationErrorCode.TOKEN_INVALID)
                    retention = await session.get(RetentionRecord, binding.batch_id)
                    self._require_retained(
                        retention, now, requested_expiry=binding.expires_at
                    )
                    source = await session.scalar(
                        select(ArtifactRecord).where(
                            ArtifactRecord.file_id == binding.file_id,
                            ArtifactRecord.batch_id == binding.batch_id,
                            ArtifactRecord.result_version
                            == binding.source_result_version,
                            ArtifactRecord.available.is_(True),
                            ArtifactRecord.deleted_at.is_(None),
                            ArtifactRecord.expires_at > now,
                        )
                    )
                    if (
                        source is None
                        or binding.expires_at > _db_utc(source.expires_at)
                    ):
                        raise OrientationFailure(OrientationErrorCode.TOKEN_INVALID)
                    duplicate = await session.scalar(
                        select(OrientationRecoveryRecord.token_digest).where(
                            OrientationRecoveryRecord.file_id == binding.file_id,
                            OrientationRecoveryRecord.source_result_version
                            == binding.source_result_version,
                        )
                    )
                    if duplicate is not None:
                        raise OrientationFailure(
                            OrientationErrorCode.REQUEST_CONFLICT
                        )
                    session.add(record)
                    await session.commit()
                    return RecoveryTokenIssue(token, self._snapshot(record))
            except OrientationFailure:
                raise
            except IntegrityError:
                if await self._source_token_exists(binding):
                    raise OrientationFailure(
                        OrientationErrorCode.REQUEST_CONFLICT
                    ) from None
                continue
            except SQLAlchemyError:
                raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None
            except Exception:
                raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None
        raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None

    async def _source_token_exists(self, binding: RecoveryTokenBinding) -> bool:
        try:
            async with self._sessions() as session:
                existing = await session.scalar(
                    select(OrientationRecoveryRecord.token_digest).where(
                        OrientationRecoveryRecord.file_id == binding.file_id,
                        OrientationRecoveryRecord.source_result_version
                        == binding.source_result_version,
                    )
                )
                return existing is not None
        except SQLAlchemyError:
            raise OrientationFailure(
                OrientationErrorCode.PERSISTENCE_FAILED
            ) from None

    async def resolve(self, token: str, *, now: datetime) -> RecoverySnapshot:
        now = _utc(now)
        digest = _digest(token)
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await session.get(OrientationRecoveryRecord, digest)
                retention = None if record is None else await session.get(
                    RetentionRecord, record.batch_id
                )
                self._require_live(record, retention, now)
                return self._snapshot(record)
        except OrientationFailure:
            raise
        except SQLAlchemyError:
            raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None
        except Exception:
            raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None

    async def claim(
        self, token: str, pages: tuple[int, ...], *, now: datetime
    ) -> RecoveryClaim:
        now = _utc(now)
        digest = _digest(token)
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await session.get(OrientationRecoveryRecord, digest)
                retention = None if record is None else await session.get(
                    RetentionRecord, record.batch_id
                )
                self._require_live(record, retention, now)
                self._snapshot(record)
                try:
                    selected = canonical_pages(pages, page_count=record.page_count)
                except ValueError:
                    raise OrientationFailure(OrientationErrorCode.REQUEST_INVALID) from None
                suspected = _decode_pages(record.suspected_pages)
                if not set(selected).issubset(suspected):
                    raise OrientationFailure(OrientationErrorCode.REQUEST_INVALID)
                fingerprint = _fingerprint(selected)
                acquired = False
                if record.state == RecoveryState.ISSUED.value:
                    record.state = RecoveryState.CLAIMED.value
                    record.selected_pages = _encode_pages(selected)
                    record.request_fingerprint = fingerprint
                    record.claim_id = "claim-" + uuid4().hex
                    record.updated_at = now
                    record.version += 1
                    acquired = True
                    await session.commit()
                elif record.request_fingerprint != fingerprint:
                    raise OrientationFailure(OrientationErrorCode.REQUEST_CONFLICT)
                snapshot = self._snapshot(record)
                return RecoveryClaim(
                    claim_id=record.claim_id,
                    request_fingerprint=fingerprint,
                    snapshot=snapshot,
                    acquired=acquired,
                )
        except OrientationFailure:
            raise
        except SQLAlchemyError:
            raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None
        except Exception:
            raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None

    async def complete(
        self,
        claim: RecoveryClaim,
        *,
        corrected_input_version: int,
        result_batch_id: str,
        result_version: int,
        now: datetime,
    ) -> RecoverySnapshot:
        if (
            not isinstance(claim, RecoveryClaim)
            or type(corrected_input_version) is not int
            or corrected_input_version <= claim.snapshot.source_result_version
            or not _valid_uuid(result_batch_id)
            or type(result_version) is not int
            or result_version <= claim.snapshot.source_result_version
        ):
            raise OrientationFailure(OrientationErrorCode.CLAIM_CONFLICT) from None
        now = _utc(now)
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await session.scalar(
                    select(OrientationRecoveryRecord).where(
                        OrientationRecoveryRecord.claim_id == claim.claim_id
                    )
                )
                retention = None if record is None else await session.get(
                    RetentionRecord, record.batch_id
                )
                if record is not None:
                    self._snapshot(record)
                self._require_claim(record, retention, claim, now)
                if await session.get(BatchRecord, result_batch_id) is None:
                    raise OrientationFailure(OrientationErrorCode.CLAIM_CONFLICT)
                expected = (corrected_input_version, result_batch_id, result_version)
                persisted = (
                    record.corrected_input_version,
                    record.result_batch_id,
                    record.result_version,
                )
                if record.state == RecoveryState.COMPLETED.value:
                    if persisted != expected:
                        raise OrientationFailure(OrientationErrorCode.CLAIM_CONFLICT)
                    return self._snapshot(record)
                if record.state != RecoveryState.CLAIMED.value:
                    raise OrientationFailure(OrientationErrorCode.CLAIM_CONFLICT)
                record.state = RecoveryState.COMPLETED.value
                record.corrected_input_version = corrected_input_version
                record.result_batch_id = result_batch_id
                record.result_version = result_version
                record.updated_at = now
                record.version += 1
                await session.commit()
                return self._snapshot(record)
        except OrientationFailure:
            raise
        except SQLAlchemyError:
            raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None
        except Exception:
            raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None

    async def fail(
        self,
        claim: RecoveryClaim,
        *,
        state: RecoveryState,
        error_code: str,
        now: datetime,
    ) -> RecoverySnapshot:
        if (
            not isinstance(claim, RecoveryClaim)
            or state not in (RecoveryState.FAILED, RecoveryState.UNCERTAIN)
            or not isinstance(error_code, str)
            or _ERROR_CODE.fullmatch(error_code) is None
        ):
            raise OrientationFailure(OrientationErrorCode.CLAIM_CONFLICT) from None
        now = _utc(now)
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await session.scalar(
                    select(OrientationRecoveryRecord).where(
                        OrientationRecoveryRecord.claim_id == claim.claim_id
                    )
                )
                retention = None if record is None else await session.get(
                    RetentionRecord, record.batch_id
                )
                if record is not None:
                    self._snapshot(record)
                self._require_claim(record, retention, claim, now)
                if record.state == state.value and record.error_code == error_code:
                    return self._snapshot(record)
                if record.state != RecoveryState.CLAIMED.value:
                    raise OrientationFailure(OrientationErrorCode.CLAIM_CONFLICT)
                record.state = state.value
                record.error_code = error_code
                record.updated_at = now
                record.version += 1
                await session.commit()
                return self._snapshot(record)
        except OrientationFailure:
            raise
        except SQLAlchemyError:
            raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None
        except Exception:
            raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None

    async def invalidate_batch(self, batch_id: str, *, now: datetime) -> int:
        if not _valid_uuid(batch_id):
            raise OrientationFailure(OrientationErrorCode.TOKEN_INVALID) from None
        now = _utc(now)
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                retention = await session.get(RetentionRecord, batch_id)
                if retention is None or not self._retention_is_non_live(retention, now):
                    raise OrientationFailure(OrientationErrorCode.TOKEN_INVALID)
                records = (
                    await session.scalars(
                        select(OrientationRecoveryRecord).where(
                            OrientationRecoveryRecord.batch_id == batch_id,
                            OrientationRecoveryRecord.state != RecoveryState.DELETED.value,
                        )
                    )
                ).all()
                for record in records:
                    record.state = RecoveryState.DELETED.value
                    record.updated_at = now
                    record.version += 1
                await session.commit()
                return len(records)
        except OrientationFailure:
            raise
        except SQLAlchemyError:
            raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None
        except Exception:
            raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None

    @staticmethod
    def _retention_is_non_live(record: RetentionRecord, now: datetime) -> bool:
        return bool(
            record.early_delete
            or record.content_deleted_at is not None
            or record.data_tombstone is not None
            or record.artifact_tombstone is not None
            or _db_utc(record.content_due_at) <= now
        )

    @staticmethod
    def _require_retained(
        record: RetentionRecord | None,
        now: datetime,
        *,
        requested_expiry: datetime | None = None,
    ) -> None:
        if (
            record is None
            or record.early_delete
            or record.content_deleted_at is not None
            or record.data_tombstone is not None
            or record.artifact_tombstone is not None
            or _db_utc(record.content_due_at) <= now
            or (
                requested_expiry is not None
                and requested_expiry > min(now + _MAX_LIFETIME, _db_utc(record.content_due_at))
            )
        ):
            raise OrientationFailure(OrientationErrorCode.TOKEN_INVALID) from None

    @classmethod
    def _require_live(
        cls,
        record: OrientationRecoveryRecord | None,
        retention: RetentionRecord | None,
        now: datetime,
    ) -> None:
        if (
            record is None
            or _db_utc(record.expires_at) <= now
            or record.state == RecoveryState.DELETED.value
        ):
            raise OrientationFailure(OrientationErrorCode.TOKEN_INVALID) from None
        cls._require_retained(retention, now)

    @staticmethod
    def _require_claim(
        record: OrientationRecoveryRecord | None,
        retention: RetentionRecord | None,
        claim: RecoveryClaim,
        now: datetime,
    ) -> None:
        if (
            record is None
            or _db_utc(record.expires_at) <= now
            or record.state == RecoveryState.DELETED.value
            or record.claim_id != claim.claim_id
            or record.request_fingerprint != claim.request_fingerprint
            or record.file_id != claim.snapshot.file_id
            or record.batch_id != claim.snapshot.batch_id
            or record.source_result_version != claim.snapshot.source_result_version
            or record.page_count != claim.snapshot.page_count
            or _decode_pages(record.suspected_pages) != claim.snapshot.suspected_pages
            or _decode_pages(record.selected_pages or "") != claim.snapshot.selected_pages
        ):
            raise OrientationFailure(OrientationErrorCode.CLAIM_CONFLICT) from None
        try:
            OrientationRecoveryRepository._require_retained(retention, now)
        except OrientationFailure:
            raise OrientationFailure(OrientationErrorCode.CLAIM_CONFLICT) from None

    @staticmethod
    def _snapshot(record: OrientationRecoveryRecord) -> RecoverySnapshot:
        try:
            return RecoverySnapshot(
                file_id=record.file_id,
                batch_id=record.batch_id,
                source_result_version=record.source_result_version,
                page_count=record.page_count,
                suspected_pages=_decode_pages(record.suspected_pages),
                selected_pages=(
                    None
                    if record.selected_pages is None
                    else _decode_pages(record.selected_pages)
                ),
                expires_at=_db_utc(record.expires_at),
                state=RecoveryState(record.state),
                request_fingerprint=record.request_fingerprint,
                claim_id=record.claim_id,
                corrected_input_version=record.corrected_input_version,
                result_batch_id=record.result_batch_id,
                result_version=record.result_version,
                error_code=record.error_code,
                version=record.version,
            )
        except OrientationFailure:
            raise
        except Exception:
            raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None
