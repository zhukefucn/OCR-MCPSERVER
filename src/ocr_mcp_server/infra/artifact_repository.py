"""Atomic SQLite index for immutable ZIPs and content-free replacement audit metadata."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
import re

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from ..domain.artifacts import (
    ArtifactBundle,
    ArtifactSnapshot,
    ReplacementAuditMetadataSnapshot,
    replacement_audit_metadata_sha256,
)
from ..domain.errors import ArtifactErrorCode, ArtifactFailure
from ..domain.merge import ReplacementAuditRecord, ReplacementDecision, ReplacementReason
from ..domain.models import SecondaryOCREngine
from ..domain.secondary_ocr import OrthogonalAngle, SecondaryResultKind
from .database import SessionFactory
from .task_models import (
    ArtifactRecord,
    FileTaskRecord,
    ReplacementAuditMetadataRecord,
)


def _fail(code: ArtifactErrorCode) -> None:
    raise ArtifactFailure(code) from None


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail(ArtifactErrorCode.INDEX_CONFLICT)
    return value.astimezone(UTC)


def _database_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _canonical_models(value) -> str:
    try:
        models = dict(value)
        safe = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+/@-]{0,127}")
        if any(
            not isinstance(key, str)
            or not isinstance(item, str)
            or safe.fullmatch(key) is None
            or safe.fullmatch(item) is None
            for key, item in models.items()
        ):
            raise ValueError
        encoded = json.dumps(models, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 4096:
            raise ValueError
        return encoded
    except BaseException:
        _fail(ArtifactErrorCode.INDEX_CONFLICT)


def _valid_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _expected_artifact_id(bundle: ArtifactBundle) -> str:
    digest = sha256(
        (
            "artifact\0"
            f"{bundle.batch_id}\0{bundle.file_task_id}\0{bundle.result_version}"
        ).encode("utf-8")
    ).hexdigest()
    return "artifact-" + digest


def _expected_storage_key(bundle: ArtifactBundle) -> str:
    return f"{bundle.artifact_id}.zip"


def _deterministic_id(value: object, prefix: str) -> bool:
    return isinstance(value, str) and re.fullmatch(re.escape(prefix) + r"[0-9a-f]{64}", value) is not None


class ArtifactRepository:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def register(
        self,
        bundle: ArtifactBundle,
        records: tuple[ReplacementAuditRecord, ...],
    ) -> ArtifactSnapshot:
        """Insert or reconcile one exact artifact and all content-free audits atomically."""

        self._validate_bundle(bundle, records)
        try:
            async with self._sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                file_record = await session.get(FileTaskRecord, bundle.file_task_id)
                if file_record is None or file_record.batch_id != bundle.batch_id:
                    _fail(ArtifactErrorCode.INDEX_CONFLICT)
                artifact = await session.get(ArtifactRecord, bundle.artifact_id)
                if artifact is None:
                    artifact = ArtifactRecord(
                        id=bundle.artifact_id,
                        file_id=bundle.file_task_id,
                        batch_id=bundle.batch_id,
                        source_version=bundle.source_version,
                        result_version=bundle.result_version,
                        storage_key=bundle.storage_key,
                        media_type=bundle.media_type,
                        size_bytes=bundle.size_bytes,
                        sha256=bundle.sha256,
                        manifest_sha256=bundle.manifest_sha256,
                        audit_metadata_sha256=bundle.audit_metadata_sha256,
                        audit_record_count=bundle.audit_record_count,
                        created_at=_utc(bundle.created_at),
                        expires_at=_utc(bundle.expires_at),
                        available=True,
                        deleted_at=None,
                        version=1,
                    )
                    session.add(artifact)
                    await session.flush()
                elif not self._artifact_matches(artifact, bundle):
                    _fail(ArtifactErrorCode.INDEX_CONFLICT)
                expected_audit_ids = {source.audit_id for source in records}
                existing_audit_ids = set(
                    (
                        await session.scalars(
                            select(ReplacementAuditMetadataRecord.audit_id).where(
                                ReplacementAuditMetadataRecord.artifact_id
                                == bundle.artifact_id
                            )
                        )
                    ).all()
                )
                if not existing_audit_ids.issubset(expected_audit_ids):
                    _fail(ArtifactErrorCode.INDEX_CONFLICT)
                for source in records:
                    existing = await session.get(ReplacementAuditMetadataRecord, source.audit_id)
                    expected = self._audit_values(bundle, source)
                    if existing is None:
                        session.add(ReplacementAuditMetadataRecord(**expected))
                    elif not self._audit_matches(existing, expected):
                        _fail(ArtifactErrorCode.INDEX_CONFLICT)
                await session.commit()
                return self._artifact_snapshot(artifact)
        except ArtifactFailure:
            raise
        except SQLAlchemyError:
            _fail(ArtifactErrorCode.INDEX_FAILED)
        except BaseException:
            _fail(ArtifactErrorCode.INDEX_FAILED)

    async def list_for_file(
        self, file_task_id: str, *, result_version: int | None = None
    ) -> tuple[ArtifactSnapshot, ...]:
        if not isinstance(file_task_id, str) or not file_task_id or (
            result_version is not None and (type(result_version) is not int or result_version < 1)
        ):
            _fail(ArtifactErrorCode.INDEX_CONFLICT)
        statement = select(ArtifactRecord).where(ArtifactRecord.file_id == file_task_id)
        if result_version is not None:
            statement = statement.where(ArtifactRecord.result_version == result_version)
        statement = statement.order_by(ArtifactRecord.result_version, ArtifactRecord.id)
        return await self._read_artifacts(statement)

    async def list_for_batch(
        self, batch_id: str, *, result_version: int | None = None
    ) -> tuple[ArtifactSnapshot, ...]:
        if not isinstance(batch_id, str) or not batch_id or (
            result_version is not None and (type(result_version) is not int or result_version < 1)
        ):
            _fail(ArtifactErrorCode.INDEX_CONFLICT)
        statement = select(ArtifactRecord).where(ArtifactRecord.batch_id == batch_id)
        if result_version is not None:
            statement = statement.where(ArtifactRecord.result_version == result_version)
        return await self._read_artifacts(statement.order_by(
            ArtifactRecord.file_id, ArtifactRecord.result_version, ArtifactRecord.id
        ))

    async def _read_artifacts(self, statement) -> tuple[ArtifactSnapshot, ...]:
        try:
            async with self._sessions() as session:
                values = (await session.scalars(statement)).all()
                return tuple(self._artifact_snapshot(value) for value in values)
        except ArtifactFailure:
            raise
        except SQLAlchemyError:
            _fail(ArtifactErrorCode.INDEX_FAILED)

    async def list_audits_for_file(
        self,
        file_task_id: str,
        *,
        output_version: int | None = None,
    ) -> tuple[ReplacementAuditMetadataSnapshot, ...]:
        if not isinstance(file_task_id, str) or not file_task_id or (
            output_version is not None and (type(output_version) is not int or output_version < 1)
        ):
            _fail(ArtifactErrorCode.INDEX_CONFLICT)
        statement = select(ReplacementAuditMetadataRecord).where(
            ReplacementAuditMetadataRecord.file_id == file_task_id
        )
        if output_version is not None:
            statement = statement.where(ReplacementAuditMetadataRecord.output_version == output_version)
        statement = statement.order_by(
            ReplacementAuditMetadataRecord.source_version,
            ReplacementAuditMetadataRecord.output_version,
            ReplacementAuditMetadataRecord.audit_id,
        )
        return await self._read_audits(statement)

    async def list_audits_for_batch(
        self,
        batch_id: str,
        *,
        output_version: int | None = None,
    ) -> tuple[ReplacementAuditMetadataSnapshot, ...]:
        if not isinstance(batch_id, str) or not batch_id or (
            output_version is not None and (type(output_version) is not int or output_version < 1)
        ):
            _fail(ArtifactErrorCode.INDEX_CONFLICT)
        statement = select(ReplacementAuditMetadataRecord).where(
            ReplacementAuditMetadataRecord.batch_id == batch_id
        )
        if output_version is not None:
            statement = statement.where(ReplacementAuditMetadataRecord.output_version == output_version)
        return await self._read_audits(statement.order_by(
            ReplacementAuditMetadataRecord.file_id,
            ReplacementAuditMetadataRecord.source_version,
            ReplacementAuditMetadataRecord.output_version,
            ReplacementAuditMetadataRecord.audit_id,
        ))

    async def _read_audits(self, statement) -> tuple[ReplacementAuditMetadataSnapshot, ...]:
        try:
            async with self._sessions() as session:
                values = (await session.scalars(statement)).all()
                return tuple(self._audit_snapshot(value) for value in values)
        except (ValueError, KeyError, TypeError):
            _fail(ArtifactErrorCode.INDEX_FAILED)
        except SQLAlchemyError:
            _fail(ArtifactErrorCode.INDEX_FAILED)

    @staticmethod
    def _validate_bundle(bundle: ArtifactBundle, records: tuple[ReplacementAuditRecord, ...]) -> None:
        if not isinstance(bundle, ArtifactBundle) or not isinstance(records, tuple):
            _fail(ArtifactErrorCode.INDEX_CONFLICT)
        try:
            expected_audit_digest = replacement_audit_metadata_sha256(
                bundle.batch_id, bundle.file_task_id, records
            )
        except BaseException:
            _fail(ArtifactErrorCode.INDEX_CONFLICT)
        reference_indexes: dict[str, int] = {}
        deterministic_records = True
        for record in records:
            if not isinstance(record, ReplacementAuditRecord):
                deterministic_records = False
                break
            expected_candidate = "candidate-" + sha256(
                (
                    "image-candidate\0"
                    f"{bundle.file_task_id}\0{bundle.source_version}\0{record.image_sha256}"
                ).encode("utf-8")
            ).hexdigest()
            expected_processing = "secondary-" + sha256(
                (
                    "secondary-record\0"
                    f"{bundle.file_task_id}\0{bundle.source_version}\0{record.image_sha256}"
                ).encode("utf-8")
            ).hexdigest()
            reference_index = reference_indexes.get(record.candidate_id, 0)
            reference_indexes[record.candidate_id] = reference_index + 1
            expected_audit = "audit-" + sha256(
                (
                    "merge-audit\0"
                    f"{bundle.file_task_id}\0{bundle.source_version}\0"
                    f"{bundle.result_version}\0{record.candidate_id}\0"
                    f"{reference_index}\0{record.reason.value}"
                ).encode("utf-8")
            ).hexdigest()
            if (
                record.candidate_id != expected_candidate
                or record.processing_record_id != expected_processing
                or record.audit_id != expected_audit
            ):
                deterministic_records = False
                break
        if (
            bundle.artifact_id != _expected_artifact_id(bundle)
            or not bundle.batch_id
            or not bundle.file_task_id
            or type(bundle.source_version) is not int
            or type(bundle.result_version) is not int
            or bundle.source_version < 1
            or bundle.result_version < 1
            or bundle.media_type != "application/zip"
            or type(bundle.size_bytes) is not int
            or bundle.size_bytes < 1
            or not _valid_hash(bundle.sha256)
            or not _valid_hash(bundle.manifest_sha256)
            or not _valid_hash(bundle.audit_metadata_sha256)
            or type(bundle.audit_record_count) is not int
            or bundle.audit_record_count != len(records)
            or expected_audit_digest != bundle.audit_metadata_sha256
            or _utc(bundle.expires_at) <= _utc(bundle.created_at)
            or bundle.storage_key != _expected_storage_key(bundle)
            or any(not isinstance(record, ReplacementAuditRecord) for record in records)
            or len(records) != bundle.replacement_count + bundle.retained_count
            or any(
                record.task_id != bundle.file_task_id
                or record.source_version != bundle.source_version
                or record.output_version != bundle.result_version
                for record in records
            )
            or len({record.audit_id for record in records}) != len(records)
            or not deterministic_records
        ):
            _fail(ArtifactErrorCode.INDEX_CONFLICT)

    @staticmethod
    def _artifact_matches(record: ArtifactRecord, bundle: ArtifactBundle) -> bool:
        return (
            record.id == bundle.artifact_id
            and record.file_id == bundle.file_task_id
            and record.batch_id == bundle.batch_id
            and record.source_version == bundle.source_version
            and record.result_version == bundle.result_version
            and record.storage_key == bundle.storage_key
            and record.media_type == bundle.media_type
            and record.size_bytes == bundle.size_bytes
            and record.sha256 == bundle.sha256
            and record.manifest_sha256 == bundle.manifest_sha256
            and record.audit_metadata_sha256 == bundle.audit_metadata_sha256
            and record.audit_record_count == bundle.audit_record_count
            and _database_utc(record.created_at) == _utc(bundle.created_at)
            and _database_utc(record.expires_at) == _utc(bundle.expires_at)
            and record.available is True
            and record.deleted_at is None
            and record.version == 1
        )

    @staticmethod
    def _audit_values(bundle: ArtifactBundle, source: ReplacementAuditRecord) -> dict:
        if (
            not _deterministic_id(source.audit_id, "audit-")
            or not _deterministic_id(source.processing_record_id, "secondary-")
            or not _deterministic_id(source.candidate_id, "candidate-")
            or not _valid_hash(source.image_sha256)
        ):
            _fail(ArtifactErrorCode.INDEX_CONFLICT)
        return {
            "audit_id": source.audit_id,
            "artifact_id": bundle.artifact_id,
            "record_id": source.processing_record_id,
            "candidate_id": source.candidate_id,
            "file_id": bundle.file_task_id,
            "batch_id": bundle.batch_id,
            "source_version": source.source_version,
            "output_version": source.output_version,
            "image_sha256": source.image_sha256,
            "decision": source.decision.value,
            "reason": source.reason.value,
            "kind": source.kind.value,
            "angle": int(source.angle),
            "confidence": source.confidence,
            "engine": source.engine.value,
            "model_versions_json": _canonical_models(source.model_versions),
            "timestamp": _utc(source.timestamp),
        }

    @staticmethod
    def _audit_matches(record: ReplacementAuditMetadataRecord, expected: dict) -> bool:
        return all(
            (
                _database_utc(getattr(record, key)) == value
                if key == "timestamp"
                else getattr(record, key) == value
            )
            for key, value in expected.items()
        )

    @staticmethod
    def _artifact_snapshot(record: ArtifactRecord) -> ArtifactSnapshot:
        return ArtifactSnapshot(
            artifact_id=record.id,
            batch_id=record.batch_id,
            file_task_id=record.file_id,
            source_version=record.source_version,
            result_version=record.result_version,
            storage_key=record.storage_key,
            media_type=record.media_type,
            size_bytes=record.size_bytes,
            sha256=record.sha256,
            manifest_sha256=record.manifest_sha256,
            audit_metadata_sha256=record.audit_metadata_sha256,
            audit_record_count=record.audit_record_count,
            created_at=_database_utc(record.created_at),
            expires_at=_database_utc(record.expires_at),
            available=record.available,
            deleted_at=None if record.deleted_at is None else _database_utc(record.deleted_at),
            version=record.version,
        )

    @staticmethod
    def _audit_snapshot(record: ReplacementAuditMetadataRecord) -> ReplacementAuditMetadataSnapshot:
        return ReplacementAuditMetadataSnapshot(
            audit_id=record.audit_id,
            artifact_id=record.artifact_id,
            record_id=record.record_id,
            candidate_id=record.candidate_id,
            file_task_id=record.file_id,
            batch_id=record.batch_id,
            source_version=record.source_version,
            output_version=record.output_version,
            image_sha256=record.image_sha256,
            decision=ReplacementDecision(record.decision),
            reason=ReplacementReason(record.reason),
            kind=SecondaryResultKind(record.kind),
            angle=OrthogonalAngle(record.angle),
            confidence=record.confidence,
            engine=SecondaryOCREngine(record.engine),
            model_versions=json.loads(record.model_versions_json),
            timestamp=_database_utc(record.timestamp),
        )
