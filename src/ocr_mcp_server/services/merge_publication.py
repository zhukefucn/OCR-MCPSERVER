"""Validated structured replacement with immutable atomic version publication."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import ctypes
from dataclasses import dataclass
from datetime import datetime
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import sys
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
from .candidate_collection import _open_candidate
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
_PUBLICATION_MANIFEST = "publication_manifest.json"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(frozen=True, slots=True)
class _TargetBinding:
    target_identity: os.stat_result
    file_identities: Mapping[str, os.stat_result]


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


def _strict_json_loads(value: str) -> object:
    def reject_duplicate_keys(pairs):
        selected = {}
        for key, item in pairs:
            if key in selected:
                raise ValueError
            selected[key] = item
        return selected

    return json.loads(
        value,
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
    )


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


def _same_object_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        stat.S_IFMT(first.st_mode),
    ) == (
        second.st_dev,
        second.st_ino,
        stat.S_IFMT(second.st_mode),
    )


def _read_verified_file(
    path: Path, *, confined_root: Path | None, max_bytes: int
) -> bytes:
    opened = None
    close_failed = False
    try:
        opened = _open_candidate(path, confined_root=confined_root)
        opened_stat = os.fstat(opened.descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_size > max_bytes
            or not _same_identity(opened.initial_name_stat, opened_stat)
        ):
            raise OSError
        chunks = []
        count = 0
        while chunk := os.read(
            opened.descriptor, min(64 * 1024, max_bytes + 1 - count)
        ):
            chunks.append(chunk)
            count += len(chunk)
            if count > max_bytes:
                raise OSError
        after_stat = os.fstat(opened.descriptor)
        after_name_stat = opened.current_name_stat()
        if (
            not _same_identity(opened_stat, after_stat)
            or not _same_identity(opened.initial_name_stat, after_name_stat)
        ):
            raise OSError
        return b"".join(chunks)
    except BaseException:
        raise OSError from None
    finally:
        if opened is not None:
            for descriptor in (opened.descriptor, opened.parent_descriptor):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        close_failed = True
        if close_failed:
            raise OSError from None


def _read_source_manifest(
    path: Path, root: Path, max_bytes: int
) -> list[list[dict]]:
    try:
        raw = _read_verified_file(path, confined_root=root, max_bytes=max_bytes)
        text = raw.decode("utf-8", errors="strict")
        value = _strict_json_loads(text)
    except BaseException:
        _fail(MergeErrorCode.INVALID_SOURCE_MANIFEST)
    if not isinstance(value, list):
        _fail(MergeErrorCode.INVALID_SOURCE_MANIFEST)
    for page in value:
        if not isinstance(page, list) or any(not isinstance(node, dict) for node in page):
            _fail(MergeErrorCode.INVALID_SOURCE_MANIFEST)
    return value


def _verify_candidate_inputs(
    result: MinerUDocumentResult, collection: CandidateCollection
) -> None:
    result_root = result.result_root.absolute()
    for candidate in collection.candidates:
        permits_standalone = any(
            reference.source_kind is CandidateSourceKind.STANDALONE_INPUT
            for reference in candidate.references
        )
        for alias in candidate.alias_paths:
            absolute_alias = alias.absolute()
            if absolute_alias.is_relative_to(result_root):
                confined_root = result.result_root
            elif permits_standalone:
                confined_root = None
            else:
                _fail(MergeErrorCode.INVARIANT_VIOLATION)
            try:
                content = _read_verified_file(
                    alias,
                    confined_root=confined_root,
                    max_bytes=candidate.size_bytes,
                )
            except BaseException:
                _fail(MergeErrorCode.INVARIANT_VIOLATION)
            if (
                len(content) != candidate.size_bytes
                or sha256(content).hexdigest() != candidate.sha256
            ):
                _fail(MergeErrorCode.INVARIANT_VIOLATION)


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
        expected_candidate_id = "candidate-" + sha256(
            (
                "image-candidate\0"
                f"{candidate.file_task_id}\0{candidate.result_version}\0{candidate.sha256}"
            ).encode("utf-8")
        ).hexdigest()
        expected_record_id = "secondary-" + sha256(
            (
                "secondary-record\0"
                f"{candidate.file_task_id}\0{candidate.result_version}\0{candidate.sha256}"
            ).encode("utf-8")
        ).hexdigest()
        if (
            not isinstance(recognized, SecondaryOcrResult)
            or candidate.candidate_id != expected_candidate_id
            or record.record_id != expected_record_id
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
            replacement["content"].pop("math_content", None)
            replacement["content"].pop("math_type", None)
            replacement["content"]["html"] = validated
            return replacement, ReplacementReason.REPLACED_TABLE
        if recognized.kind is SecondaryResultKind.FORMULA and recognized.content_format is SecondaryContentFormat.LATEX:
            validated = validate_formula_latex(recognized.content, limits)
            replacement = deepcopy(node)
            replacement["type"] = "equation_interline"
            replacement["content"].pop("html", None)
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


def _bind_publication_contents(
    files: Mapping[str, bytes],
    *,
    artifact_kind: str,
    task_id: str,
    source_version: int,
    output_version: int,
    rolled_back_merge_version: int | None = None,
) -> dict[str, bytes]:
    metadata = {
        "artifact_kind": artifact_kind,
        "task_id": task_id,
        "source_version": source_version,
        "output_version": output_version,
        "files": {
            name: sha256(content).hexdigest()
            for name, content in sorted(files.items())
        },
    }
    if rolled_back_merge_version is not None:
        metadata["rolled_back_merge_version"] = rolled_back_merge_version
    return {**files, _PUBLICATION_MANIFEST: _canonical_json(metadata)}


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


def _write_file_anchored(
    stage: Path, stage_descriptor: int | None, name: str, content: bytes
) -> None:
    if stage_descriptor is None:
        _write_file(stage / name, content)
        return
    descriptor = os.open(
        name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=stage_descriptor,
    )
    transferred = False
    try:
        output = os.fdopen(descriptor, "wb")
        transferred = True
        with output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if not transferred:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _publish_stage_anchored(
    stage: Path, target: Path, root_descriptor: int | None
) -> None:
    if root_descriptor is None or not sys.platform.startswith("linux"):
        publish_directory_no_replace(stage, target)
        return
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOTSUP, "atomic no-replace publication is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        root_descriptor,
        os.fsencode(stage.name),
        root_descriptor,
        os.fsencode(target.name),
        1,
    )
    error_number = ctypes.get_errno()
    if result == 0:
        return
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number))
    raise OSError(error_number, os.strerror(error_number))


def _existing_matches(
    target: Path, expected: Mapping[str, bytes]
) -> _TargetBinding | None:
    try:
        target_stat = os.lstat(target)
        if not stat.S_ISDIR(target_stat.st_mode) or stat.S_ISLNK(target_stat.st_mode) or _is_reparse(target_stat):
            return None
        if {item.name for item in target.iterdir()} != set(expected):
            return None
        identities = {}
        for name, content in expected.items():
            path = target / name
            path_stat = os.lstat(path)
            if (
                not stat.S_ISREG(path_stat.st_mode)
                or stat.S_ISLNK(path_stat.st_mode)
                or _is_reparse(path_stat)
                or path_stat.st_size != len(content)
                or _read_verified_file(
                    path,
                    confined_root=target,
                    max_bytes=len(content),
                )
                != content
                or not _same_identity(os.lstat(path), path_stat)
                or not _same_object_identity(os.lstat(target), target_stat)
            ):
                return None
            identities[name] = path_stat
        return _TargetBinding(target_stat, identities)
    except BaseException:
        return None


def _existing_target_is_unsafe(target: Path) -> bool:
    try:
        target_stat = os.lstat(target)
        if (
            not stat.S_ISDIR(target_stat.st_mode)
            or stat.S_ISLNK(target_stat.st_mode)
            or _is_reparse(target_stat)
        ):
            return True
        for item in target.iterdir():
            item_stat = os.lstat(item)
            if (
                not stat.S_ISREG(item_stat.st_mode)
                or stat.S_ISLNK(item_stat.st_mode)
                or _is_reparse(item_stat)
            ):
                return True
        return False
    except BaseException:
        return True


def _assert_publication_root_identity(
    root: Path, expected_identity: os.stat_result
) -> None:
    try:
        if not _same_object_identity(os.lstat(root), expected_identity):
            _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
    except MergeFailure:
        raise
    except BaseException:
        _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)


def _classify_existing_target_anchored(
    root_descriptor: int, target_name: str, expected: Mapping[str, bytes]
) -> tuple[str, _TargetBinding | None]:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        target_descriptor = os.open(
            target_name, directory_flags, dir_fd=root_descriptor
        )
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unsafe", None
    try:
        target_identity = os.fstat(target_descriptor)
        identities = {}
        names = set(os.listdir(target_descriptor))
        for name in names:
            item_stat = os.stat(
                name, dir_fd=target_descriptor, follow_symlinks=False
            )
            if not stat.S_ISREG(item_stat.st_mode) or _is_reparse(item_stat):
                return "unsafe", None
        if names != set(expected):
            return "conflict", None
        for name, content in expected.items():
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=target_descriptor,
            )
            try:
                initial_stat = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(initial_stat.st_mode)
                    or initial_stat.st_size != len(content)
                ):
                    return "conflict", None
                chunks = []
                count = 0
                while chunk := os.read(
                    descriptor, min(64 * 1024, len(content) + 1 - count)
                ):
                    chunks.append(chunk)
                    count += len(chunk)
                    if count > len(content):
                        return "conflict", None
                if b"".join(chunks) != content or not _same_identity(
                    initial_stat, os.fstat(descriptor)
                ):
                    return "conflict", None
                if not _same_identity(
                    initial_stat,
                    os.stat(
                        name,
                        dir_fd=target_descriptor,
                        follow_symlinks=False,
                    ),
                ):
                    return "unsafe", None
                identities[name] = initial_stat
            finally:
                os.close(descriptor)
        if not _same_object_identity(
            target_identity,
            os.stat(
                target_name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            ),
        ):
            return "unsafe", None
        return "match", _TargetBinding(target_identity, identities)
    except OSError:
        return "unsafe", None
    finally:
        os.close(target_descriptor)


def _assert_target_binding_path(target: Path, binding: _TargetBinding) -> None:
    try:
        if not _same_object_identity(os.lstat(target), binding.target_identity):
            _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
        for name, identity in binding.file_identities.items():
            if not _same_identity(os.lstat(target / name), identity):
                _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
        if not _same_object_identity(os.lstat(target), binding.target_identity):
            _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
    except MergeFailure:
        raise
    except BaseException:
        _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)


def _assert_target_binding_anchored(
    root_descriptor: int, target_name: str, binding: _TargetBinding
) -> None:
    try:
        target_stat = os.stat(
            target_name, dir_fd=root_descriptor, follow_symlinks=False
        )
        if not _same_object_identity(target_stat, binding.target_identity):
            _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
        target_descriptor = os.open(
            target_name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_descriptor,
        )
        try:
            for name, identity in binding.file_identities.items():
                if not _same_identity(
                    os.stat(name, dir_fd=target_descriptor, follow_symlinks=False),
                    identity,
                ):
                    _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
            if not _same_object_identity(
                os.stat(
                    target_name,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                ),
                binding.target_identity,
            ):
                _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
        finally:
            os.close(target_descriptor)
    except MergeFailure:
        raise
    except BaseException:
        _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)


def _verify_new_publication(
    target: Path,
    contents: Mapping[str, bytes],
    *,
    original_stage_identity: os.stat_result,
    root_descriptor: int | None,
) -> _TargetBinding:
    if root_descriptor is not None:
        status, binding = _classify_existing_target_anchored(
            root_descriptor, target.name, contents
        )
        if (
            status != "match"
            or binding is None
            or not _same_object_identity(
                binding.target_identity, original_stage_identity
            )
        ):
            _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
        _assert_target_binding_anchored(
            root_descriptor, target.name, binding
        )
        return binding
    if _existing_target_is_unsafe(target):
        _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
    binding = _existing_matches(target, contents)
    if (
        binding is None
        or not _same_object_identity(
            binding.target_identity, original_stage_identity
        )
    ):
        _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
    _assert_target_binding_path(target, binding)
    return binding


def _finalize_publication_return(
    root: Path,
    root_identity: os.stat_result,
    target: Path,
    binding: _TargetBinding,
    root_descriptor: int | None,
) -> Path:
    _assert_publication_root_identity(root, root_identity)
    if root_descriptor is not None:
        _assert_target_binding_anchored(
            root_descriptor, target.name, binding
        )
    else:
        _assert_target_binding_path(target, binding)
    _assert_publication_root_identity(root, root_identity)
    return target


def _publish(root: Path, output_version: int, contents: Mapping[str, bytes], max_artifact_bytes: int) -> Path:
    root = _ensure_safe_directory(root, create=True)
    root_identity = os.lstat(root)
    if type(output_version) is not int or output_version < 1 or any(len(value) > max_artifact_bytes for value in contents.values()):
        _fail(MergeErrorCode.INVARIANT_VIOLATION)
    target = root / f"version-{output_version:08d}"
    root_descriptor = None
    if os.name != "nt":
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        root_descriptor = os.open(root, directory_flags)
        if not _same_object_identity(os.fstat(root_descriptor), root_identity):
            os.close(root_descriptor)
            _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
        existing_status, existing_binding = _classify_existing_target_anchored(
            root_descriptor, target.name, contents
        )
        if existing_status != "missing":
            try:
                _assert_publication_root_identity(root, root_identity)
                if existing_status == "unsafe":
                    _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
                if existing_status == "match":
                    assert existing_binding is not None
                    return _finalize_publication_return(
                        root,
                        root_identity,
                        target,
                        existing_binding,
                        root_descriptor,
                    )
                _fail(MergeErrorCode.PUBLICATION_CONFLICT)
            finally:
                os.close(root_descriptor)
    elif target.exists() or target.is_symlink():
        if _existing_target_is_unsafe(target):
            _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
        _assert_publication_root_identity(root, root_identity)
        existing_binding = _existing_matches(target, contents)
        if existing_binding is not None:
            return _finalize_publication_return(
                root,
                root_identity,
                target,
                existing_binding,
                root_descriptor,
            )
        _fail(MergeErrorCode.PUBLICATION_CONFLICT)
    try:
        stage = Path(tempfile.mkdtemp(prefix=".merge-stage-", dir=root))
    except BaseException:
        if root_descriptor is not None:
            try:
                os.close(root_descriptor)
            except OSError:
                pass
        _fail(MergeErrorCode.PUBLICATION_FAILED)
    stage_identity = os.lstat(stage)
    stage_descriptor = None
    try:
        _ensure_safe_directory(stage, create=False)
        if root_descriptor is not None:
            stage_descriptor = os.open(
                stage.name, directory_flags, dir_fd=root_descriptor
            )
            if (
                not _same_object_identity(os.fstat(root_descriptor), root_identity)
                or not _same_object_identity(os.fstat(stage_descriptor), stage_identity)
            ):
                _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
        for name, content in contents.items():
            if (
                not _same_object_identity(os.lstat(root), root_identity)
                or not _same_object_identity(os.lstat(stage), stage_identity)
            ):
                _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
            _write_file_anchored(stage, stage_descriptor, name, content)
        try:
            if stage_descriptor is not None:
                os.fsync(stage_descriptor)
            else:
                directory_fd = os.open(stage, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError:
            if os.name != "nt":
                raise
        if (
            not _same_object_identity(os.lstat(root), root_identity)
            or not _same_object_identity(os.lstat(stage), stage_identity)
        ):
            _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
        _publish_stage_anchored(stage, target, root_descriptor)
        publication_binding = _verify_new_publication(
            target,
            contents,
            original_stage_identity=(
                os.fstat(stage_descriptor)
                if stage_descriptor is not None
                else stage_identity
            ),
            root_descriptor=root_descriptor,
        )
        try:
            root_fd = root_descriptor
            owns_root_fd = False
            if root_fd is None:
                root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                owns_root_fd = True
            try:
                if not _same_object_identity(os.fstat(root_fd), root_identity):
                    _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
                os.fsync(root_fd)
            finally:
                if owns_root_fd:
                    os.close(root_fd)
        except OSError:
            if os.name != "nt":
                raise
        return _finalize_publication_return(
            root,
            root_identity,
            target,
            publication_binding,
            root_descriptor,
        )
    except FileExistsError:
        if root_descriptor is not None:
            existing_status, existing_binding = _classify_existing_target_anchored(
                root_descriptor, target.name, contents
            )
            _assert_publication_root_identity(root, root_identity)
            if existing_status == "unsafe":
                _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
            if existing_status == "match":
                assert existing_binding is not None
                return _finalize_publication_return(
                    root,
                    root_identity,
                    target,
                    existing_binding,
                    root_descriptor,
                )
        else:
            if _existing_target_is_unsafe(target):
                _fail(MergeErrorCode.UNSAFE_PUBLICATION_PATH)
            _assert_publication_root_identity(root, root_identity)
            existing_binding = _existing_matches(target, contents)
            if existing_binding is not None:
                return _finalize_publication_return(
                    root,
                    root_identity,
                    target,
                    existing_binding,
                    root_descriptor,
                )
        _fail(MergeErrorCode.PUBLICATION_CONFLICT)
    except MergeFailure:
        raise
    except BaseException:
        _fail(MergeErrorCode.PUBLICATION_FAILED)
    finally:
        if stage_descriptor is not None:
            try:
                os.close(stage_descriptor)
            except OSError:
                pass
        if root_descriptor is not None:
            try:
                os.close(root_descriptor)
            except OSError:
                pass


def _merge_and_publish_impl(
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
    manifest = _read_source_manifest(
        result.content_list_v2_path,
        result.result_root,
        limits.max_artifact_bytes,
    )
    _verify_candidate_inputs(result, collection)
    final, records = _merge_manifest(result, collection, bound, manifest, output_version, timestamp, limits)
    original_bytes = _canonical_json(manifest)
    manifest_bytes = _canonical_json(final)
    audit_bytes = _canonical_json(_audit_document(collection.file_task_id, collection.result_version, output_version, records))
    files = dict(
        zip(
            _REQUIRED_FILES,
            (original_bytes, manifest_bytes, audit_bytes),
            strict=True,
        )
    )
    contents = _bind_publication_contents(
        files,
        artifact_kind="merge",
        task_id=collection.file_task_id,
        source_version=collection.result_version,
        output_version=output_version,
    )
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

    try:
        return _merge_and_publish_impl(
            result,
            collection,
            results,
            publication_root=publication_root,
            output_version=output_version,
            timestamp=timestamp,
            limits=limits,
        )
    except MergeFailure:
        raise
    except BaseException:
        _fail(MergeErrorCode.INVARIANT_VIOLATION)


def _verified_rollback_source(
    publication: MergePublicationResult, root: Path, *, max_artifact_bytes: int
) -> bytes:
    try:
        expected_directory = root / f"version-{publication.output_version:08d}"
        if publication.publication_directory.absolute() != expected_directory.absolute():
            _fail(MergeErrorCode.ROLLBACK_VERIFICATION_FAILED)
        _ensure_safe_directory(expected_directory, create=False)
        fixed_paths = (
            expected_directory / _REQUIRED_FILES[0],
            expected_directory / _REQUIRED_FILES[1],
            expected_directory / _REQUIRED_FILES[2],
        )
        if (
            publication.original_snapshot_path.absolute() != fixed_paths[0].absolute()
            or publication.manifest_path.absolute() != fixed_paths[1].absolute()
            or publication.audit_path.absolute() != fixed_paths[2].absolute()
            or {item.name for item in expected_directory.iterdir()}
            != {*_REQUIRED_FILES, _PUBLICATION_MANIFEST}
        ):
            _fail(MergeErrorCode.ROLLBACK_VERIFICATION_FAILED)
        metadata_bytes = _read_verified_file(
            expected_directory / _PUBLICATION_MANIFEST,
            confined_root=expected_directory,
            max_bytes=max_artifact_bytes,
        )
        metadata = _strict_json_loads(metadata_bytes.decode("utf-8"))
        if (
            not isinstance(metadata, dict)
            or set(metadata)
            != {
                "artifact_kind",
                "task_id",
                "source_version",
                "output_version",
                "files",
            }
            or metadata.get("artifact_kind") != "merge"
            or metadata.get("task_id") != publication.task_id
            or metadata.get("source_version") != publication.source_version
            or metadata.get("output_version") != publication.output_version
            or not isinstance(metadata.get("files"), dict)
            or set(metadata["files"]) != set(_REQUIRED_FILES)
            or _canonical_json(metadata) != metadata_bytes
        ):
            _fail(MergeErrorCode.ROLLBACK_VERIFICATION_FAILED)
        values = (
            (fixed_paths[0], publication.original_sha256),
            (fixed_paths[1], publication.manifest_sha256),
            (fixed_paths[2], publication.audit_sha256),
        )
        loaded = []
        for path, expected_hash in values:
            content = _read_verified_file(
                path,
                confined_root=expected_directory,
                max_bytes=max_artifact_bytes,
            )
            content_hash = sha256(content).hexdigest()
            if (
                content_hash != expected_hash
                or metadata["files"].get(path.name) != content_hash
            ):
                _fail(MergeErrorCode.ROLLBACK_VERIFICATION_FAILED)
            loaded.append(content)
        original = _strict_json_loads(loaded[0].decode("utf-8"))
        final = _strict_json_loads(loaded[1].decode("utf-8"))
        audit = _strict_json_loads(loaded[2].decode("utf-8"))
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


def _rollback_publication_impl(
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
    original_bytes = _verified_rollback_source(
        publication,
        root,
        max_artifact_bytes=limits.max_artifact_bytes,
    )
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
    files = {
        "original_content_list_v2.json": original_bytes,
        "content_list_v2.json": original_bytes,
        "secondary_ocr_audit.json": rollback_audit,
    }
    contents = _bind_publication_contents(
        files,
        artifact_kind="rollback",
        task_id=publication.task_id,
        source_version=publication.source_version,
        output_version=output_version,
        rolled_back_merge_version=publication.output_version,
    )
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


def rollback_publication(
    publication: MergePublicationResult,
    *,
    publication_root: Path,
    output_version: int,
    timestamp: datetime,
    limits: StructuredContentLimits,
) -> RollbackPublicationResult:
    """Publish a new version restored from a verified preserved original snapshot."""

    try:
        return _rollback_publication_impl(
            publication,
            publication_root=publication_root,
            output_version=output_version,
            timestamp=timestamp,
            limits=limits,
        )
    except MergeFailure:
        raise
    except BaseException:
        _fail(MergeErrorCode.ROLLBACK_VERIFICATION_FAILED)
