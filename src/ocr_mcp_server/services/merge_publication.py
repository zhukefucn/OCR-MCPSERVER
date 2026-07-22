"""Validated structured replacement with immutable atomic version publication."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import unicodedata

from ..domain import (
    CandidateCollection,
    CandidateReference,
    CandidateSourceKind,
    MergeErrorCode,
    MergeFailure,
    MergePublicationResult,
    MinerUDocumentResult,
    ReplacementAuditRecord,
    ReplacementDecision,
    ReplacementReason,
    RollbackPublicationResult,
    SecondaryContentFormat,
    SecondaryOcrResult,
    SecondaryResultKind,
    SecondaryResultState,
)
from ..infra.mineru_archive import publish_directory_no_replace
from .structured_content import (
    StructuredContentInvalid,
    StructuredContentLimits,
    validate_formula_latex,
    validate_table_html,
)


_WINDOWS_DEVICES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_REQUIRED_FILES = (
    "original_content_list_v2.json",
    "content_list_v2.json",
    "secondary_ocr_audit.json",
)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _fail(code: MergeErrorCode) -> None:
    raise MergeFailure(code) from None


def _canonical_json(value: object) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _fail(MergeErrorCode.INVALID_SOURCE_MANIFEST)


def _canonical_snapshot(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, UnicodeError):
        _fail(MergeErrorCode.INVALID_SOURCE_MANIFEST)


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        stat.S_IFMT(first.st_mode),
        first.st_size,
        first.st_mtime_ns,
        first.st_ctime_ns,
    ) == (
        second.st_dev,
        second.st_ino,
        stat.S_IFMT(second.st_mode),
        second.st_size,
        second.st_mtime_ns,
        second.st_ctime_ns,
    )


def _read_source_manifest(path: Path, max_bytes: int) -> list[list[dict]]:
    descriptor = -1
    try:
        name_stat = os.lstat(path)
        if stat.S_ISLNK(name_stat.st_mode) or _is_reparse(name_stat):
            raise OSError
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_size > max_bytes
            or not _same_identity(name_stat, opened_stat)
        ):
            raise OSError
        chunks = []
        count = 0
        while chunk := os.read(descriptor, min(64 * 1024, max_bytes + 1 - count)):
            chunks.append(chunk)
            count += len(chunk)
            if count > max_bytes:
                raise OSError
        after_stat = os.fstat(descriptor)
        after_name_stat = os.lstat(path)
        if not _same_identity(opened_stat, after_stat) or not _same_identity(name_stat, after_name_stat):
            raise OSError
        raw = b"".join(chunks)
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except BaseException:
        _fail(MergeErrorCode.INVALID_SOURCE_MANIFEST)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                _fail(MergeErrorCode.INVALID_SOURCE_MANIFEST)
    if not isinstance(value, list):
        _fail(MergeErrorCode.INVALID_SOURCE_MANIFEST)
    for page in value:
        if not isinstance(page, list) or any(not isinstance(node, dict) for node in page):
            _fail(MergeErrorCode.INVALID_SOURCE_MANIFEST)
    return value


def _safe_posix_parts(value: object) -> tuple[str, ...] | None:
    if (
        not isinstance(value, str) or not value or value.startswith("/")
        or value.startswith("//") or "\\" in value or re.match(r"^[A-Za-z]:($|/)", value)
    ):
        return None
    parts = tuple(value.split("/"))
    for part in parts:
        if (
            not part or part in {".", ".."} or ":" in part
            or part != part.rstrip(" .")
            or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in part)
            or part.split(".", 1)[0].upper() in _WINDOWS_DEVICES
        ):
            return None
    return parts


def _known_alias(result: MinerUDocumentResult, candidate, raw_path: object) -> bool:
    try:
        parts = _safe_posix_parts(raw_path)
        if parts is None:
            return False
        expected = result.content_list_v2_path.parent.joinpath(*parts).absolute()
        root = result.images_directory.absolute()
        if not expected.is_relative_to(root):
            return False
        return any(path.absolute() == expected for path in candidate.alias_paths)
    except BaseException:
        return False


def _validate_inputs(
    result: MinerUDocumentResult,
    collection: CandidateCollection,
    results: Mapping[str, SecondaryOcrResult],
    output_version: int,
    timestamp: datetime,
) -> dict[str, tuple[object, object, SecondaryOcrResult]]:
    if (
        not isinstance(result, MinerUDocumentResult)
        or not isinstance(collection, CandidateCollection)
        or result.file_task_id != collection.file_task_id
        or type(output_version) is not int
        or output_version <= collection.result_version
        or not isinstance(timestamp, datetime)
        or timestamp.tzinfo is None
        or timestamp.utcoffset() is None
        or not isinstance(results, Mapping)
    ):
        _fail(MergeErrorCode.INVARIANT_VIOLATION)
    candidates = {candidate.candidate_id: candidate for candidate in collection.candidates}
    records = {record.candidate_id: record for record in collection.processing_records}
    if set(results) != set(candidates) or set(records) != set(candidates):
        _fail(MergeErrorCode.INVALID_COVERAGE)
    bound = {}
    pointers: set[str] = set()
    for candidate_id, candidate in candidates.items():
        record = records[candidate_id]
        recognized = results[candidate_id]
        if (
            not isinstance(recognized, SecondaryOcrResult)
            or candidate.file_task_id != collection.file_task_id
            or record.file_task_id != collection.file_task_id
            or candidate.result_version != collection.result_version
            or record.result_version != collection.result_version
            or record.candidate_id != candidate.candidate_id
            or record.engine is not recognized.engine
        ):
            _fail(MergeErrorCode.INVARIANT_VIOLATION)
        for reference in candidate.references:
            if reference.source_kind is CandidateSourceKind.MINERU_NODE:
                assert reference.json_pointer is not None
                if reference.json_pointer in pointers:
                    _fail(MergeErrorCode.INVARIANT_VIOLATION)
                pointers.add(reference.json_pointer)
        bound[candidate_id] = (candidate, record, recognized)
    return bound


def _existing_structure_is_valid(node: dict, limits: StructuredContentLimits) -> bool:
    content = node.get("content")
    if not isinstance(content, dict):
        return False
    try:
        if node.get("type") == "table":
            validate_table_html(content.get("html"), limits)
            return True
        if node.get("type") == "equation_interline" and content.get("math_type") == "latex":
            validate_formula_latex(content.get("math_content"), limits)
            return True
    except StructuredContentInvalid:
        return False
    return False


def _result_reason(recognized: SecondaryOcrResult) -> ReplacementReason | None:
    if recognized.state is SecondaryResultState.UNCERTAIN:
        return ReplacementReason.UNCERTAIN
    if recognized.state is SecondaryResultState.FAILED:
        return ReplacementReason.FAILED
    if recognized.state is SecondaryResultState.INVALID:
        return ReplacementReason.INVALID_RESULT
    if recognized.state is not SecondaryResultState.VALID:
        return ReplacementReason.INVALID_RESULT
    if recognized.kind is SecondaryResultKind.OTHER:
        return ReplacementReason.OTHER_IMAGE
    if recognized.kind is SecondaryResultKind.UNCERTAIN:
        return ReplacementReason.UNCERTAIN
    return None


def _replace_node(node: dict, recognized: SecondaryOcrResult, limits: StructuredContentLimits) -> tuple[dict | None, ReplacementReason]:
    preliminary = _result_reason(recognized)
    if preliminary is not None:
        return None, preliminary
    try:
        if recognized.kind is SecondaryResultKind.TABLE and recognized.content_format is SecondaryContentFormat.HTML:
            validated = validate_table_html(recognized.content, limits)
            replacement = deepcopy(node)
            replacement["type"] = "table"
            replacement["content"]["html"] = validated
            return replacement, ReplacementReason.REPLACED_TABLE
        if recognized.kind is SecondaryResultKind.FORMULA and recognized.content_format is SecondaryContentFormat.LATEX:
            validated = validate_formula_latex(recognized.content, limits)
            replacement = deepcopy(node)
            replacement["type"] = "equation_interline"
            replacement["content"]["math_content"] = validated
            replacement["content"]["math_type"] = "latex"
            return replacement, ReplacementReason.REPLACED_FORMULA
    except StructuredContentInvalid:
        return None, ReplacementReason.INVALID_CONTENT
    return None, ReplacementReason.INVALID_RESULT


def _audit_id(task_id: str, source_version: int, output_version: int, candidate_id: str, reference_index: int, reason: ReplacementReason) -> str:
    digest = sha256((f"merge-audit\0{task_id}\0{source_version}\0{output_version}\0{candidate_id}\0{reference_index}\0{reason.value}").encode("utf-8")).hexdigest()
    return f"audit-{digest}"


def _audit_record(
    *, result, collection, output_version, candidate, record, recognized,
    reference: CandidateReference, reference_index: int, decision, reason,
    timestamp, original: dict | None, replacement: dict | None,
) -> ReplacementAuditRecord:
    return ReplacementAuditRecord(
        audit_id=_audit_id(collection.file_task_id, collection.result_version, output_version, candidate.candidate_id, reference_index, reason),
        task_id=collection.file_task_id,
        source_version=collection.result_version,
        output_version=output_version,
        candidate_id=candidate.candidate_id,
        processing_record_id=record.record_id,
        image_sha256=candidate.sha256,
        json_pointer=reference.json_pointer,
        page_index=reference.page_index,
        node_index=reference.node_index,
        original_node_type=reference.original_node_type,
        kind=recognized.kind,
        angle=recognized.angle,
        confidence=recognized.confidence,
        engine=recognized.engine,
        model_versions=recognized.model_versions,
        decision=decision,
        reason=reason,
        timestamp=timestamp,
        original_node_snapshot=_canonical_snapshot(original) if original is not None else None,
        replacement_node_snapshot=_canonical_snapshot(replacement) if replacement is not None else None,
    )


def _merge_manifest(result, collection, bound, manifest, output_version, timestamp, limits):
    final = deepcopy(manifest)
    audits = []
    for candidate in collection.candidates:
        _, record, recognized = bound[candidate.candidate_id]
        for reference_index, reference in enumerate(candidate.references):
            original = replacement = None
            decision = ReplacementDecision.RETAINED
            if reference.source_kind is CandidateSourceKind.STANDALONE_INPUT:
                reason = ReplacementReason.STANDALONE_REFERENCE
            else:
                page_index, node_index = reference.page_index, reference.node_index
                stale = (
                    type(page_index) is not int or type(node_index) is not int
                    or page_index >= len(final) or node_index >= len(final[page_index])
                )
                if not stale:
                    node = final[page_index][node_index]
                    original = deepcopy(node)
                    content = node.get("content")
                    image_source = content.get("image_source") if isinstance(content, dict) else None
                    raw_path = image_source.get("path") if isinstance(image_source, dict) else None
                    stale = (
                        reference.json_pointer != f"/{page_index}/{node_index}"
                        or node.get("type") != reference.original_node_type
                        or not _known_alias(result, candidate, raw_path)
                    )
                if stale:
                    reason = ReplacementReason.STALE_REFERENCE
                elif _existing_structure_is_valid(node, limits):
                    reason = ReplacementReason.ALREADY_STRUCTURED
                else:
                    replacement, reason = _replace_node(node, recognized, limits)
                    if replacement is not None:
                        final[page_index][node_index] = replacement
                        decision = ReplacementDecision.REPLACED
            audits.append(_audit_record(
                result=result, collection=collection, output_version=output_version,
                candidate=candidate, record=record, recognized=recognized,
                reference=reference, reference_index=reference_index,
                decision=decision, reason=reason, timestamp=timestamp,
                original=original, replacement=replacement,
            ))
    return final, tuple(audits)


def _audit_document(task_id: str, source_version: int, output_version: int, records: tuple[ReplacementAuditRecord, ...]) -> dict:
    return {
        "task_id": task_id,
        "source_version": source_version,
        "output_version": output_version,
        "records": [
            {
                "audit_id": item.audit_id,
                "task_id": item.task_id,
                "source_version": item.source_version,
                "output_version": item.output_version,
                "candidate_id": item.candidate_id,
                "processing_record_id": item.processing_record_id,
                "image_sha256": item.image_sha256,
                "json_pointer": item.json_pointer,
                "page_index": item.page_index,
                "node_index": item.node_index,
                "original_node_type": item.original_node_type,
                "kind": item.kind.value,
                "angle": int(item.angle),
                "confidence": item.confidence,
                "engine": item.engine.value,
                "model_versions": dict(sorted(item.model_versions.items())),
                "decision": item.decision.value,
                "reason": item.reason.value,
                "timestamp": item.timestamp.isoformat(),
                "original_node_snapshot": item.original_node_snapshot,
                "replacement_node_snapshot": item.replacement_node_snapshot,
            }
            for item in records
        ],
    }


def _is_reparse(path_stat: os.stat_result) -> bool:
    return bool(getattr(path_stat, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _assert_safe_directory_chain(path: Path) -> None:
    current = path.absolute()
    chain = []
    while True:
        chain.append(current)
        if current.parent == current:
            break
        current = current.parent
    for component in reversed(chain):
        try:
            component_stat = os.lstat(component)
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISDIR(component_stat.st_mode)
            or stat.S_ISLNK(component_stat.st_mode)
            or _is_reparse(component_stat)
        ):
            _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)


def _ensure_safe_directory(path: Path, *, create: bool) -> Path:
    try:
        path = path.absolute()
        _assert_safe_directory_chain(path)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        _assert_safe_directory_chain(path)
        path_stat = os.lstat(path)
        if not stat.S_ISDIR(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode) or _is_reparse(path_stat):
            _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
        return path
    except MergeFailure:
        raise
    except BaseException:
        _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)


def _write_file(path: Path, content: bytes) -> None:
    with path.open("xb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _safe_rmtree(stage: Path, root: Path) -> None:
    try:
        if stage.parent == root and stage.name.startswith(".merge-stage-"):
            stage_stat = os.lstat(stage)
            if stat.S_ISDIR(stage_stat.st_mode) and not stat.S_ISLNK(stage_stat.st_mode) and not _is_reparse(stage_stat):
                shutil.rmtree(stage)
    except BaseException:
        pass


def _existing_matches(target: Path, expected: Mapping[str, bytes]) -> bool:
    try:
        target_stat = os.lstat(target)
        if not stat.S_ISDIR(target_stat.st_mode) or stat.S_ISLNK(target_stat.st_mode) or _is_reparse(target_stat):
            return False
        if {item.name for item in target.iterdir()} != set(expected):
            return False
        for name, content in expected.items():
            path = target / name
            path_stat = os.lstat(path)
            if not stat.S_ISREG(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode) or _is_reparse(path_stat) or path.read_bytes() != content:
                return False
        return True
    except BaseException:
        return False


def _publish(root: Path, output_version: int, contents: Mapping[str, bytes], max_artifact_bytes: int) -> Path:
    root = _ensure_safe_directory(root, create=True)
    if type(output_version) is not int or output_version < 1 or any(len(value) > max_artifact_bytes for value in contents.values()):
        _fail(MergeErrorCode.INVARIANT_VIOLATION)
    target = root / f"version-{output_version:08d}"
    if target.exists() or target.is_symlink():
        if _existing_matches(target, contents):
            return target
        _fail(MergeErrorCode.PUBLICATION_CONFLICT)
    stage = Path(tempfile.mkdtemp(prefix=".merge-stage-", dir=root))
    published = False
    try:
        _ensure_safe_directory(stage, create=False)
        for name, content in contents.items():
            _write_file(stage / name, content)
        try:
            directory_fd = os.open(stage, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            if os.name != "nt":
                raise
        publish_directory_no_replace(stage, target)
        published = True
        return target
    except FileExistsError:
        if _existing_matches(target, contents):
            return target
        _fail(MergeErrorCode.PUBLICATION_CONFLICT)
    except MergeFailure:
        raise
    except BaseException:
        _fail(MergeErrorCode.PUBLICATION_FAILED)
    finally:
        if not published and stage.exists():
            _safe_rmtree(stage, root)


def merge_and_publish(
    result: MinerUDocumentResult,
    collection: CandidateCollection,
    results: Mapping[str, SecondaryOcrResult],
    *,
    publication_root: Path,
    output_version: int,
    timestamp: datetime,
    limits: StructuredContentLimits,
) -> MergePublicationResult:
    """Validate, merge, audit, and atomically publish one immutable result version."""

    bound = _validate_inputs(result, collection, results, output_version, timestamp)
    if not isinstance(limits, StructuredContentLimits):
        _fail(MergeErrorCode.INVARIANT_VIOLATION)
    manifest = _read_source_manifest(result.content_list_v2_path, limits.max_artifact_bytes)
    final, records = _merge_manifest(result, collection, bound, manifest, output_version, timestamp, limits)
    original_bytes = _canonical_json(manifest)
    manifest_bytes = _canonical_json(final)
    audit_bytes = _canonical_json(_audit_document(collection.file_task_id, collection.result_version, output_version, records))
    contents = dict(zip(_REQUIRED_FILES, (original_bytes, manifest_bytes, audit_bytes), strict=True))
    target = _publish(Path(publication_root), output_version, contents, limits.max_artifact_bytes)
    replacement_count = sum(item.decision is ReplacementDecision.REPLACED for item in records)
    return MergePublicationResult(
        task_id=collection.file_task_id,
        source_version=collection.result_version,
        output_version=output_version,
        publication_directory=target,
        original_snapshot_path=target / _REQUIRED_FILES[0],
        manifest_path=target / _REQUIRED_FILES[1],
        audit_path=target / _REQUIRED_FILES[2],
        records=records,
        replacement_count=replacement_count,
        retained_count=len(records) - replacement_count,
        original_sha256=sha256(original_bytes).hexdigest(),
        manifest_sha256=sha256(manifest_bytes).hexdigest(),
        audit_sha256=sha256(audit_bytes).hexdigest(),
    )


def _verified_rollback_source(publication: MergePublicationResult, root: Path) -> bytes:
    try:
        expected_directory = root / f"version-{publication.output_version:08d}"
        if publication.publication_directory.absolute() != expected_directory.absolute():
            _fail(MergeErrorCode.ROLLBACK_VERIFICATION_FAILED)
        _ensure_safe_directory(expected_directory, create=False)
        values = (
            (publication.original_snapshot_path, publication.original_sha256),
            (publication.manifest_path, publication.manifest_sha256),
            (publication.audit_path, publication.audit_sha256),
        )
        loaded = []
        for path, expected_hash in values:
            if path.parent.absolute() != expected_directory.absolute():
                _fail(MergeErrorCode.ROLLBACK_VERIFICATION_FAILED)
            path_stat = os.lstat(path)
            if not stat.S_ISREG(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode) or _is_reparse(path_stat):
                _fail(MergeErrorCode.ROLLBACK_VERIFICATION_FAILED)
            content = path.read_bytes()
            if sha256(content).hexdigest() != expected_hash:
                _fail(MergeErrorCode.ROLLBACK_VERIFICATION_FAILED)
            loaded.append(content)
        original = json.loads(loaded[0].decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        final = json.loads(loaded[1].decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        audit = json.loads(loaded[2].decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        def valid_manifest(value: object) -> bool:
            return isinstance(value, list) and all(
                isinstance(page, list) and all(isinstance(node, dict) for node in page)
                for page in value
            )
        if not valid_manifest(original) or not valid_manifest(final):
            _fail(MergeErrorCode.ROLLBACK_VERIFICATION_FAILED)
        audit_records = audit.get("records") if isinstance(audit, dict) else None
        if (
            not isinstance(audit, dict)
            or audit.get("task_id") != publication.task_id
            or audit.get("source_version") != publication.source_version
            or audit.get("output_version") != publication.output_version
            or not isinstance(audit_records, list)
            or [item.get("audit_id") for item in audit_records if isinstance(item, dict)]
            != [item.audit_id for item in publication.records]
            or len(audit_records) != len(publication.records)
        ):
            _fail(MergeErrorCode.ROLLBACK_VERIFICATION_FAILED)
        return loaded[0]
    except MergeFailure:
        raise
    except BaseException:
        _fail(MergeErrorCode.ROLLBACK_VERIFICATION_FAILED)


def rollback_publication(
    publication: MergePublicationResult,
    *,
    publication_root: Path,
    output_version: int,
    timestamp: datetime,
    limits: StructuredContentLimits,
) -> RollbackPublicationResult:
    """Publish a new version restored from a verified preserved original snapshot."""

    if (
        not isinstance(publication, MergePublicationResult)
        or type(output_version) is not int
        or output_version <= publication.output_version
        or not isinstance(timestamp, datetime)
        or timestamp.tzinfo is None
        or timestamp.utcoffset() is None
    ):
        _fail(MergeErrorCode.INVARIANT_VIOLATION)
    root = _ensure_safe_directory(Path(publication_root), create=False)
    original_bytes = _verified_rollback_source(publication, root)
    record_ids = tuple(record.audit_id for record in publication.records)
    rollback_audit = _canonical_json({
        "task_id": publication.task_id,
        "source_version": publication.source_version,
        "rolled_back_merge_version": publication.output_version,
        "output_version": output_version,
        "record_ids": list(record_ids),
        "reason": "restore_preserved_original",
        "timestamp": timestamp.isoformat(),
    })
    contents = {
        "original_content_list_v2.json": original_bytes,
        "content_list_v2.json": original_bytes,
        "secondary_ocr_audit.json": rollback_audit,
    }
    target = _publish(root, output_version, contents, limits.max_artifact_bytes)
    return RollbackPublicationResult(
        task_id=publication.task_id,
        source_version=publication.source_version,
        rolled_back_merge_version=publication.output_version,
        output_version=output_version,
        publication_directory=target,
        manifest_path=target / "content_list_v2.json",
        audit_path=target / "secondary_ocr_audit.json",
        manifest_sha256=sha256(original_bytes).hexdigest(),
        audit_sha256=sha256(rollback_audit).hexdigest(),
        record_ids=record_ids,
    )
