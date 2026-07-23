"""Content-free durable results for one-time whole-page orientation assessment."""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from ..domain.orientation import (
    OrientationAssessmentClaim,
    OrientationAssessmentSnapshot,
    OrientationAssessmentState,
    OrientationErrorCode,
    OrientationFailure,
)
from .database import SessionFactory
from .task_models import FileTaskRecord, OrientationAssessmentRecord


_ERROR_CODE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")


def _utc(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise OrientationFailure(OrientationErrorCode.REQUEST_INVALID) from None
    return value.astimezone(UTC)


def _db_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _encode_pages(pages: tuple[int, ...]) -> str:
    return ",".join(str(page) for page in pages)


def _decode_pages(value: str) -> tuple[int, ...]:
    if value == "":
        return ()
    try:
        return tuple(int(page) for page in value.split(","))
    except (TypeError, ValueError):
        raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None


class OrientationAssessmentRepository:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def begin(
        self,
        *,
        file_id: str,
        batch_id: str,
        result_version: int,
        page_count: int,
        now: datetime,
        lease_seconds: int,
    ) -> OrientationAssessmentClaim | None:
        now = _utc(now)
        if (
            not isinstance(file_id, str)
            or not file_id
            or not isinstance(batch_id, str)
            or not batch_id
            or type(result_version) is not int
            or result_version < 1
            or type(page_count) is not int
            or page_count < 1
            or type(lease_seconds) is not int
            or lease_seconds < 1
        ):
            raise OrientationFailure(OrientationErrorCode.REQUEST_INVALID) from None
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                file_record = await session.get(FileTaskRecord, file_id)
                if file_record is None or file_record.batch_id != batch_id:
                    raise OrientationFailure(OrientationErrorCode.REQUEST_INVALID)
                existing = await session.scalar(
                    select(OrientationAssessmentRecord).where(
                        OrientationAssessmentRecord.file_id == file_id,
                        OrientationAssessmentRecord.result_version == result_version,
                    )
                )
                if existing is not None:
                    if (
                        existing.batch_id != batch_id
                        or existing.page_count != page_count
                    ):
                        raise OrientationFailure(
                            OrientationErrorCode.REQUEST_CONFLICT
                        )
                    if existing.state != OrientationAssessmentState.DETECTING.value:
                        await session.commit()
                        return None
                    lease_expires_at = (
                        None
                        if existing.lease_expires_at is None
                        else _db_utc(existing.lease_expires_at)
                    )
                    if lease_expires_at is not None and lease_expires_at > now:
                        await session.commit()
                        return None
                    self._assign_claim(existing, now, lease_seconds)
                    await session.commit()
                    return self._claim(existing)
                record = OrientationAssessmentRecord(
                    file_id=file_id,
                    batch_id=batch_id,
                    result_version=result_version,
                    page_count=page_count,
                    state=OrientationAssessmentState.DETECTING.value,
                    suspected_pages="",
                    error_code=None,
                    claim_token=None,
                    lease_expires_at=None,
                    attempt_count=0,
                    created_at=now,
                    updated_at=now,
                )
                self._assign_claim(record, now, lease_seconds)
                session.add(record)
                await session.commit()
                return self._claim(record)
        except OrientationFailure:
            raise
        except IntegrityError:
            return None
        except SQLAlchemyError:
            raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None

    async def complete(
        self,
        claim: OrientationAssessmentClaim,
        *,
        suspected_pages: tuple[int, ...],
        now: datetime,
    ) -> OrientationAssessmentSnapshot:
        now = _utc(now)
        try:
            pages = tuple(sorted(suspected_pages))
        except TypeError:
            raise OrientationFailure(OrientationErrorCode.REQUEST_INVALID) from None
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await self._require_claim(session, claim, now)
                if (
                    any(
                        type(page) is not int
                        or page < 1
                        or page > record.page_count
                        for page in pages
                    )
                    or len(set(pages)) != len(pages)
                ):
                    raise OrientationFailure(OrientationErrorCode.REQUEST_INVALID)
                record.state = (
                    OrientationAssessmentState.READY.value
                    if pages
                    else OrientationAssessmentState.NO_SUSPICION.value
                )
                record.suspected_pages = _encode_pages(pages)
                record.claim_token = None
                record.lease_expires_at = None
                record.updated_at = now
                await session.commit()
                return self._snapshot(record)
        except OrientationFailure:
            raise
        except SQLAlchemyError:
            raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None

    async def fail(
        self,
        claim: OrientationAssessmentClaim,
        *,
        error_code: str,
        now: datetime,
    ) -> OrientationAssessmentSnapshot:
        now = _utc(now)
        if not isinstance(error_code, str) or _ERROR_CODE.fullmatch(error_code) is None:
            raise OrientationFailure(OrientationErrorCode.REQUEST_INVALID) from None
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                record = await self._require_claim(session, claim, now)
                record.state = OrientationAssessmentState.FAILED.value
                record.error_code = error_code
                record.claim_token = None
                record.lease_expires_at = None
                record.updated_at = now
                await session.commit()
                return self._snapshot(record)
        except OrientationFailure:
            raise
        except SQLAlchemyError:
            raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None

    async def get(
        self, file_id: str, result_version: int
    ) -> OrientationAssessmentSnapshot | None:
        if (
            not isinstance(file_id, str)
            or not file_id
            or type(result_version) is not int
            or result_version < 1
        ):
            raise OrientationFailure(OrientationErrorCode.REQUEST_INVALID) from None
        try:
            async with self._sessions() as session:
                record = await session.scalar(
                    select(OrientationAssessmentRecord).where(
                        OrientationAssessmentRecord.file_id == file_id,
                        OrientationAssessmentRecord.result_version == result_version,
                    )
                )
                return None if record is None else self._snapshot(record)
        except OrientationFailure:
            raise
        except SQLAlchemyError:
            raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None

    @staticmethod
    async def _require_claim(
        session,
        claim: OrientationAssessmentClaim,
        now: datetime,
    ):
        if not isinstance(claim, OrientationAssessmentClaim):
            raise OrientationFailure(OrientationErrorCode.REQUEST_CONFLICT)
        record = await session.scalar(
            select(OrientationAssessmentRecord).where(
                OrientationAssessmentRecord.file_id == claim.file_id,
                OrientationAssessmentRecord.result_version
                == claim.result_version,
            )
        )
        if (
            record is None
            or record.state != OrientationAssessmentState.DETECTING.value
            or record.claim_token != claim.claim_token
            or record.attempt_count != claim.attempt
            or record.lease_expires_at is None
            or _db_utc(record.lease_expires_at) != claim.lease_expires_at
            or _db_utc(record.lease_expires_at) <= now
        ):
            raise OrientationFailure(OrientationErrorCode.REQUEST_CONFLICT)
        return record

    @staticmethod
    def _assign_claim(
        record: OrientationAssessmentRecord,
        now: datetime,
        lease_seconds: int,
    ) -> None:
        record.claim_token = "oa-" + secrets.token_hex(16)
        record.lease_expires_at = now + timedelta(seconds=lease_seconds)
        record.attempt_count += 1
        record.updated_at = now

    @staticmethod
    def _claim(
        record: OrientationAssessmentRecord,
    ) -> OrientationAssessmentClaim:
        if record.claim_token is None or record.lease_expires_at is None:
            raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED)
        return OrientationAssessmentClaim(
            file_id=record.file_id,
            result_version=record.result_version,
            claim_token=record.claim_token,
            lease_expires_at=_db_utc(record.lease_expires_at),
            attempt=record.attempt_count,
        )

    @staticmethod
    def _snapshot(
        record: OrientationAssessmentRecord,
    ) -> OrientationAssessmentSnapshot:
        try:
            return OrientationAssessmentSnapshot(
                file_id=record.file_id,
                batch_id=record.batch_id,
                result_version=record.result_version,
                page_count=record.page_count,
                state=OrientationAssessmentState(record.state),
                suspected_pages=_decode_pages(record.suspected_pages),
                error_code=record.error_code,
                attempt_count=record.attempt_count,
                created_at=_db_utc(record.created_at),
                updated_at=_db_utc(record.updated_at),
            )
        except OrientationFailure:
            raise
        except Exception:
            raise OrientationFailure(OrientationErrorCode.PERSISTENCE_FAILED) from None
