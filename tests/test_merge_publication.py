from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from collections.abc import Mapping
import shutil

import pytest

from ocr_mcp_server.domain import (
    CandidateCollection,
    CandidateReference,
    CandidateSourceKind,
    ImageCandidate,
    MergeErrorCode,
    MergeFailure,
    MinerUDocumentResult,
    MinerUImageFormat,
    OrthogonalAngle,
    ReplacementDecision,
    ReplacementReason,
    SecondaryContentFormat,
    SecondaryOCREngine,
    SecondaryOcrResult,
    SecondaryProcessingRecord,
    SecondaryResultKind,
    SecondaryResultState,
)
from ocr_mcp_server.services.merge_publication import (
    _write_file_anchored,
    merge_and_publish,
    rollback_publication,
)
from ocr_mcp_server.services.structured_content import StructuredContentLimits


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)


@pytest.fixture
def limits() -> StructuredContentLimits:
    return StructuredContentLimits(
        max_characters=10_000,
        max_utf8_bytes=20_000,
        max_html_depth=16,
        max_html_elements=100,
        max_table_rows=20,
        max_table_cells=100,
        max_latex_repetition=20,
        max_artifact_bytes=1_000_000,
    )


def _setup(tmp_path: Path, nodes: list[dict], *, references=None):
    source = tmp_path / "mineru"
    images = source / "images"
    images.mkdir(parents=True)
    image = images / "a.png"
    image.write_bytes(b"immutable-image")
    manifest_path = source / "doc_content_list_v2.json"
    manifest_path.write_text(json.dumps([nodes]), encoding="utf-8")
    result = MinerUDocumentResult(
        file_task_id="file-123",
        upstream_task_id="upstream-1",
        result_root=source,
        markdown_path=None,
        middle_json_path=None,
        content_list_v2_path=manifest_path,
        legacy_content_list_path=None,
        images_directory=images,
    )
    if references is None:
        references = (
            CandidateReference(
                source_kind=CandidateSourceKind.MINERU_NODE,
                page_index=0,
                node_index=0,
                json_pointer="/0/0",
                original_node_type=nodes[0]["type"],
            ),
        )
    image_sha256 = sha256(b"immutable-image").hexdigest()
    candidate_id = "candidate-" + sha256(
        f"image-candidate\0file-123\0{1}\0{image_sha256}".encode("utf-8")
    ).hexdigest()
    candidate = ImageCandidate(
        candidate_id=candidate_id,
        file_task_id="file-123",
        result_version=1,
        sha256=image_sha256,
        size_bytes=15,
        image_format=MinerUImageFormat.PNG,
        width=10,
        height=10,
        primary_path=image,
        alias_paths=(image,),
        references=tuple(references),
        node_type_hints=tuple(dict.fromkeys(r.original_node_type for r in references if r.original_node_type)),
    )
    record = SecondaryProcessingRecord.pending_for(candidate, engine=SecondaryOCREngine.PP_STRUCTURE_V3)
    collection = CandidateCollection(
        file_task_id="file-123",
        result_version=1,
        candidates=(candidate,),
        processing_records=(record,),
    )
    return result, collection, candidate, manifest_path.read_bytes()


def _ocr(kind=SecondaryResultKind.TABLE, content="<table><tr><td>识别</td></tr></table>", *, state=SecondaryResultState.VALID):
    return SecondaryOcrResult(
        kind=kind,
        angle=OrthogonalAngle.DEG_90,
        content=content if kind in {SecondaryResultKind.TABLE, SecondaryResultKind.FORMULA} else None,
        content_format=(SecondaryContentFormat.HTML if kind is SecondaryResultKind.TABLE else SecondaryContentFormat.LATEX if kind is SecondaryResultKind.FORMULA else None),
        confidence=0.98,
        engine=SecondaryOCREngine.PP_STRUCTURE_V3,
        model_versions={"pipeline": "trusted-v1"},
        state=state,
    )


def test_table_replacement_publishes_immutable_version_and_audit(tmp_path, limits):
    original_node = {"type": "image", "bbox": [1, 2, 3, 4], "content": {"image_source": {"path": "images/a.png"}, "note": "safe"}}
    result, collection, candidate, source_bytes = _setup(tmp_path, [original_node])
    source_object = json.loads(result.content_list_v2_path.read_text(encoding="utf-8"))
    publication = merge_and_publish(
        result, collection, {candidate.candidate_id: _ocr()},
        publication_root=tmp_path / "published", output_version=2,
        timestamp=NOW, limits=limits,
    )
    final = json.loads(publication.manifest_path.read_text(encoding="utf-8"))
    node = final[0][0]
    assert node["type"] == "table"
    assert node["content"]["image_source"] == {"path": "images/a.png"}
    assert node["content"]["note"] == "safe"
    assert node["content"]["html"].startswith("<table>")
    assert publication.source_version == 1 and publication.output_version == 2
    assert publication.replacement_count == 1 and publication.retained_count == 0
    assert publication.records[0].decision is ReplacementDecision.REPLACED
    assert publication.records[0].reason is ReplacementReason.REPLACED_TABLE
    assert json.loads(publication.records[0].original_node_snapshot) == original_node
    assert result.content_list_v2_path.read_bytes() == source_bytes
    assert source_object == json.loads(result.content_list_v2_path.read_text(encoding="utf-8"))


def test_formula_replacement_preserves_fields(tmp_path, limits):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}, "keep": 7}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    publication = merge_and_publish(
        result, collection, {candidate.candidate_id: _ocr(SecondaryResultKind.FORMULA, r"x_{i}")},
        publication_root=tmp_path / "published", output_version=2, timestamp=NOW, limits=limits,
    )
    changed = json.loads(publication.manifest_path.read_text(encoding="utf-8"))[0][0]
    assert changed == {"type": "equation_interline", "content": {"image_source": {"path": "images/a.png"}, "math_content": r"x_{i}", "math_type": "latex"}, "keep": 7}
    assert publication.records[0].reason is ReplacementReason.REPLACED_FORMULA


@pytest.mark.parametrize(
    "ocr,reason",
    [
        (_ocr(SecondaryResultKind.OTHER, None), ReplacementReason.OTHER_IMAGE),
        (_ocr(state=SecondaryResultState.UNCERTAIN), ReplacementReason.UNCERTAIN),
        (_ocr(state=SecondaryResultState.FAILED), ReplacementReason.FAILED),
        (_ocr(state=SecondaryResultState.INVALID), ReplacementReason.INVALID_RESULT),
        (_ocr(content="<table><tr><td></td></tr></table>"), ReplacementReason.INVALID_CONTENT),
    ],
)
def test_noneligible_results_retain_exact_node(tmp_path, limits, ocr, reason):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    publication = merge_and_publish(result, collection, {candidate.candidate_id: ocr}, publication_root=tmp_path / "published", output_version=2, timestamp=NOW, limits=limits)
    assert json.loads(publication.manifest_path.read_text(encoding="utf-8"))[0][0] == node
    assert publication.records[0].reason is reason


@pytest.mark.parametrize(
    "node,ocr",
    [
        ({"type": "table", "content": {"image_source": {"path": "images/a.png"}, "html": "<table><tr><td>原始</td></tr></table>"}}, _ocr()),
        ({"type": "equation_interline", "content": {"image_source": {"path": "images/a.png"}, "math_content": "x+y", "math_type": "latex"}}, _ocr(SecondaryResultKind.FORMULA, "z")),
    ],
)
def test_valid_existing_structure_is_diagnostic_only(tmp_path, limits, node, ocr):
    result, collection, candidate, _ = _setup(tmp_path, [node])
    publication = merge_and_publish(result, collection, {candidate.candidate_id: ocr}, publication_root=tmp_path / "published", output_version=2, timestamp=NOW, limits=limits)
    assert json.loads(publication.manifest_path.read_text(encoding="utf-8"))[0][0] == node
    assert publication.records[0].reason is ReplacementReason.ALREADY_STRUCTURED


def test_invalid_existing_structure_can_be_replaced(tmp_path, limits):
    node = {"type": "table", "content": {"image_source": {"path": "images/a.png"}, "html": "<table></table>"}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    publication = merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=tmp_path / "published", output_version=2, timestamp=NOW, limits=limits)
    assert publication.replacement_count == 1


@pytest.mark.parametrize(
    ("node", "recognized", "removed"),
    [
        (
            {"type": "table", "content": {"image_source": {"path": "images/a.png"}, "html": "<script>unsafe</script>"}},
            _ocr(SecondaryResultKind.FORMULA, "x+y"),
            {"html"},
        ),
        (
            {"type": "equation_interline", "content": {"image_source": {"path": "images/a.png"}, "math_content": r"\input{x}", "math_type": "latex"}},
            _ocr(),
            {"math_content", "math_type"},
        ),
    ],
)
def test_cross_type_replacement_removes_old_unsafe_structured_fields(tmp_path, limits, node, recognized, removed):
    result, collection, candidate, _ = _setup(tmp_path, [node])
    publication = merge_and_publish(result, collection, {candidate.candidate_id: recognized}, publication_root=tmp_path / "published", output_version=2, timestamp=NOW, limits=limits)
    content = json.loads(publication.manifest_path.read_text(encoding="utf-8"))[0][0]["content"]
    assert removed.isdisjoint(content)


def test_dangerous_formula_payload_retains_node_end_to_end(tmp_path, limits):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    publication = merge_and_publish(result, collection, {candidate.candidate_id: _ocr(SecondaryResultKind.FORMULA, r"\includegraphics{../../secret}")}, publication_root=tmp_path / "published", output_version=2, timestamp=NOW, limits=limits)
    assert publication.records[0].reason is ReplacementReason.INVALID_CONTENT
    assert json.loads(publication.manifest_path.read_text(encoding="utf-8"))[0][0] == node


def test_stale_reference_and_standalone_are_each_audited_without_fake_nodes(tmp_path, limits):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    refs = (
        CandidateReference(source_kind=CandidateSourceKind.MINERU_NODE, page_index=0, node_index=0, json_pointer="/0/0", original_node_type="chart"),
        CandidateReference.standalone_input(),
    )
    result, collection, candidate, _ = _setup(tmp_path, [node], references=refs)
    publication = merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=tmp_path / "published", output_version=2, timestamp=NOW, limits=limits)
    assert [record.reason for record in publication.records] == [ReplacementReason.STALE_REFERENCE, ReplacementReason.STANDALONE_REFERENCE]
    assert publication.records[1].json_pointer is None


def test_one_candidate_updates_every_real_reference_in_deterministic_order(tmp_path, limits):
    nodes = [
        {"type": "image", "content": {"image_source": {"path": "images/a.png"}}, "slot": 1},
        {"type": "chart", "content": {"image_source": {"path": "images/a.png"}}, "slot": 2},
    ]
    refs = (
        CandidateReference(source_kind=CandidateSourceKind.MINERU_NODE, page_index=0, node_index=0, json_pointer="/0/0", original_node_type="image"),
        CandidateReference(source_kind=CandidateSourceKind.MINERU_NODE, page_index=0, node_index=1, json_pointer="/0/1", original_node_type="chart"),
    )
    result, collection, candidate, _ = _setup(tmp_path, nodes, references=refs)
    publication = merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=tmp_path / "published", output_version=2, timestamp=NOW, limits=limits)
    final = json.loads(publication.manifest_path.read_text(encoding="utf-8"))[0]
    assert [node["type"] for node in final] == ["table", "table"]
    assert [record.json_pointer for record in publication.records] == ["/0/0", "/0/1"]
    assert publication.replacement_count == 2
    assert len({record.audit_id for record in publication.records}) == 2


def test_source_manifest_symlink_is_rejected_without_publication(tmp_path, limits):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    actual = result.content_list_v2_path.with_name("actual.json")
    result.content_list_v2_path.replace(actual)
    try:
        result.content_list_v2_path.symlink_to(actual)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(MergeFailure) as raised:
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=tmp_path / "published", output_version=2, timestamp=NOW, limits=limits)
    assert raised.value.code == MergeErrorCode.INVALID_SOURCE_MANIFEST.value
    assert not (tmp_path / "published" / "version-00000002").exists()


def test_source_manifest_parent_symlink_is_rejected(tmp_path, limits):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    original_root = result.result_root.with_name("mineru-original")
    result.result_root.replace(original_root)
    attacker_root = tmp_path / "attacker"; attacker_root.mkdir()
    (attacker_root / "images").mkdir()
    (attacker_root / "images" / "a.png").write_bytes(b"attacker-image")
    (attacker_root / result.content_list_v2_path.name).write_text(json.dumps([[node]]), encoding="utf-8")
    try:
        result.result_root.symlink_to(attacker_root, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(MergeFailure) as raised:
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=tmp_path / "published", output_version=2, timestamp=NOW, limits=limits)
    assert raised.value.code == MergeErrorCode.INVALID_SOURCE_MANIFEST.value


def test_candidate_content_changed_after_collection_fails_globally(tmp_path, limits):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    candidate.primary_path.write_bytes(b"changed-after-inference")
    with pytest.raises(MergeFailure) as raised:
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=tmp_path / "published", output_version=2, timestamp=NOW, limits=limits)
    assert raised.value.code == MergeErrorCode.INVARIANT_VIOLATION.value
    assert not (tmp_path / "published" / "version-00000002").exists()


@pytest.mark.parametrize("mutation", ["missing", "extra", "engine", "task", "version", "duplicate_pointer"])
def test_global_coverage_and_identity_mismatch_publishes_nothing(tmp_path, limits, mutation):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    results = {candidate.candidate_id: _ocr()}
    if mutation == "missing": results = {}
    elif mutation == "extra": results["extra"] = _ocr()
    elif mutation == "engine": results[candidate.candidate_id] = replace(_ocr(), engine=SecondaryOCREngine.PADDLEOCR_VL)
    elif mutation == "task": result = replace(result, file_task_id="other")
    elif mutation == "version": pass
    elif mutation == "duplicate_pointer":
        other = replace(candidate, candidate_id="candidate-2", sha256="b" * 64)
        other_record = SecondaryProcessingRecord.pending_for(other, engine=SecondaryOCREngine.PP_STRUCTURE_V3)
        collection = CandidateCollection(file_task_id="file-123", result_version=1, candidates=(candidate, other), processing_records=(collection.processing_records[0], other_record))
        results[other.candidate_id] = _ocr()
    with pytest.raises((MergeFailure, ValueError)):
        merge_and_publish(result, collection, results, publication_root=tmp_path / "published", output_version=1 if mutation == "version" else 2, timestamp=NOW, limits=limits)
    assert not (tmp_path / "published" / "version-00000002").exists()


@pytest.mark.parametrize("identity_kind", ["candidate", "record"])
def test_forged_deterministic_candidate_or_record_identity_is_rejected(tmp_path, limits, identity_kind):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    record = collection.processing_records[0]
    if identity_kind == "candidate":
        forged_id = "candidate-" + "f" * 64
        candidate = replace(candidate, candidate_id=forged_id)
        record = replace(record, candidate_id=forged_id)
    else:
        record = replace(record, record_id="secondary-" + "f" * 64)
    collection = CandidateCollection(
        file_task_id=collection.file_task_id,
        result_version=collection.result_version,
        candidates=(candidate,),
        processing_records=(record,),
    )
    with pytest.raises(MergeFailure) as raised:
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=tmp_path / "published", output_version=2, timestamp=NOW, limits=limits)
    assert raised.value.code == MergeErrorCode.INVARIANT_VIOLATION.value
    assert not (tmp_path / "published" / "version-00000002").exists()


def test_malformed_manifest_fails_safely_and_does_not_leak(tmp_path, limits, caplog):
    planted = "SECRET_PATH_AND_JSON"
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    result.content_list_v2_path.write_text(planted, encoding="utf-8")
    with pytest.raises(MergeFailure) as raised:
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr(content=f"<table><tr><td>{planted}</td></tr></table>")}, publication_root=tmp_path / "published", output_version=2, timestamp=NOW, limits=limits)
    assert raised.value.code == MergeErrorCode.INVALID_SOURCE_MANIFEST.value
    assert planted not in str(raised.value)
    assert raised.value.__cause__ is None
    assert planted not in caplog.text


def test_duplicate_json_object_keys_are_rejected_as_ambiguous(tmp_path, limits):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    result.content_list_v2_path.write_text(
        '[[{"type":"image","type":"chart","content":{"image_source":{"path":"images/a.png"}}}]]',
        encoding="utf-8",
    )
    with pytest.raises(MergeFailure) as raised:
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=tmp_path / "published", output_version=2, timestamp=NOW, limits=limits)
    assert raised.value.code == MergeErrorCode.INVALID_SOURCE_MANIFEST.value


def test_hostile_result_mapping_error_is_safely_discarded(tmp_path, limits, caplog):
    planted = "SECRET_BACKEND_MAPPING_ERROR"
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, _, _ = _setup(tmp_path, [node])

    class HostileMapping(Mapping):
        def __getitem__(self, key): raise RuntimeError(planted)
        def __iter__(self): raise RuntimeError(planted)
        def __len__(self): raise RuntimeError(planted)

    with pytest.raises(MergeFailure) as raised:
        merge_and_publish(result, collection, HostileMapping(), publication_root=tmp_path / "published", output_version=2, timestamp=NOW, limits=limits)
    assert planted not in str(raised.value)
    assert raised.value.__cause__ is None
    assert planted not in caplog.text


def test_idempotent_retry_verifies_bytes_and_conflict_is_safe(tmp_path, limits):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    kwargs = dict(publication_root=tmp_path / "published", output_version=2, timestamp=NOW, limits=limits)
    first = merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, **kwargs)
    second = merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, **kwargs)
    assert first.manifest_sha256 == second.manifest_sha256
    first.manifest_path.write_text("{}", encoding="utf-8")
    with pytest.raises(MergeFailure) as raised:
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, **kwargs)
    assert raised.value.code == MergeErrorCode.PUBLICATION_CONFLICT.value


def test_idempotent_retry_with_extra_regular_file_is_conflict(tmp_path, limits):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    root = tmp_path / "published"
    kwargs = dict(publication_root=root, output_version=2, timestamp=NOW, limits=limits)
    publication = merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, **kwargs)
    (publication.publication_directory / "extra.json").write_text("{}", encoding="utf-8")
    with pytest.raises(MergeFailure) as raised:
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, **kwargs)
    assert raised.value.code == MergeErrorCode.PUBLICATION_CONFLICT.value


def test_idempotent_retry_rejects_publication_root_swap_between_checks(tmp_path, limits, monkeypatch):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    root = tmp_path / "published"
    kwargs = dict(publication_root=root, output_version=2, timestamp=NOW, limits=limits)
    merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, **kwargs)
    alternate = tmp_path / "alternate-publication"
    shutil.copytree(root, alternate)
    saved = tmp_path / "saved-publication"
    module = __import__("ocr_mcp_server.services.merge_publication", fromlist=["_assert_target_binding_path"])
    binding_check_name = (
        "_assert_target_binding_path"
        if os.name == "nt"
        else "_assert_target_binding_anchored"
    )
    original_check = getattr(module, binding_check_name)
    swapped = False

    def swap_after_final_binding(*args, **kwargs):
        nonlocal swapped
        answer = original_check(*args, **kwargs)
        if not swapped:
            root.rename(saved)
            alternate.rename(root)
            swapped = True
        return answer

    monkeypatch.setattr(module, binding_check_name, swap_after_final_binding)
    with pytest.raises(MergeFailure) as raised:
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, **kwargs)
    assert raised.value.code == MergeErrorCode.UNSAFE_PUBLICATION_PATH.value


def test_idempotent_retry_rejects_target_swap_after_byte_match(tmp_path, limits, monkeypatch):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    root = tmp_path / "published"
    kwargs = dict(publication_root=root, output_version=2, timestamp=NOW, limits=limits)
    publication = merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, **kwargs)
    target = publication.publication_directory
    alternate = root / "alternate-version"
    shutil.copytree(target, alternate)
    content_path = alternate / "content_list_v2.json"
    altered = bytearray(content_path.read_bytes()); altered[0] = ord("{")
    content_path.write_bytes(bytes(altered))
    saved = root / "saved-version"
    module = __import__("ocr_mcp_server.services.merge_publication", fromlist=["_existing_matches"])
    swapped = False

    if os.name == "nt":
        original_match = module._existing_matches

        def swap_after_match(path, expected):
            nonlocal swapped
            answer = original_match(path, expected)
            if answer and not swapped:
                path.rename(saved)
                alternate.rename(path)
                swapped = True
            return answer

        monkeypatch.setattr(module, "_existing_matches", swap_after_match)
    else:
        original_open = module.os.open
        target_open_count = 0

        def swap_after_final_target_open(path, *args, **kwargs):
            nonlocal swapped, target_open_count
            descriptor = original_open(path, *args, **kwargs)
            if path == target.name and kwargs.get("dir_fd") is not None:
                target_open_count += 1
                if target_open_count == 2 and not swapped:
                    target.rename(saved)
                    alternate.rename(target)
                    swapped = True
            return descriptor

        monkeypatch.setattr(module.os, "open", swap_after_final_target_open)
    with pytest.raises(MergeFailure) as raised:
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, **kwargs)
    assert raised.value.code == MergeErrorCode.UNSAFE_PUBLICATION_PATH.value


def test_publish_failure_preserves_stage_as_orphan(tmp_path, limits, monkeypatch):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    root = tmp_path / "published"; root.mkdir(); unrelated = root / ".staging-unrelated"; unrelated.mkdir()
    monkeypatch.setattr("ocr_mcp_server.services.merge_publication._publish_stage_anchored", lambda *_: (_ for _ in ()).throw(OSError("SECRET")))
    with pytest.raises(MergeFailure):
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=root, output_version=2, timestamp=NOW, limits=limits)
    assert unrelated.exists()
    orphans = list(root.glob(".merge-stage-*"))
    assert len(orphans) == 1
    assert {item.name for item in orphans[0].iterdir()} == {
        "original_content_list_v2.json",
        "content_list_v2.json",
        "secondary_ocr_audit.json",
        "publication_manifest.json",
    }


def test_publish_failure_does_not_delete_replacement_at_staging_path(tmp_path, limits, monkeypatch):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    root = tmp_path / "published"; root.mkdir()
    replacement = {"path": None}

    def replace_stage_then_fail(stage, target, root_descriptor):
        del target, root_descriptor
        moved = stage.with_name(stage.name + "-owned")
        stage.rename(moved)
        stage.mkdir()
        marker = stage / "attacker-marker"
        marker.write_text("keep", encoding="utf-8")
        replacement["path"] = stage
        raise OSError("injected")

    monkeypatch.setattr("ocr_mcp_server.services.merge_publication._publish_stage_anchored", replace_stage_then_fail)
    with pytest.raises(MergeFailure):
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=root, output_version=2, timestamp=NOW, limits=limits)
    assert replacement["path"].is_dir()
    assert (replacement["path"] / "attacker-marker").read_text(encoding="utf-8") == "keep"


def test_stage_name_swap_before_rename_never_returns_publication_success(tmp_path, limits, monkeypatch):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    root = tmp_path / "published"; root.mkdir()
    module = __import__("ocr_mcp_server.services.merge_publication", fromlist=["_publish_stage_anchored"])
    original_publish = module._publish_stage_anchored

    def swap_stage_name(stage, target, root_descriptor):
        owned = stage.with_name(stage.name + "-owned")
        stage.rename(owned)
        shutil.copytree(owned, stage)
        content = stage / "content_list_v2.json"
        altered = bytearray(content.read_bytes()); altered[0] = ord("{")
        content.write_bytes(bytes(altered))
        return original_publish(stage, target, root_descriptor)

    monkeypatch.setattr("ocr_mcp_server.services.merge_publication._publish_stage_anchored", swap_stage_name)
    with pytest.raises(MergeFailure):
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=root, output_version=2, timestamp=NOW, limits=limits)


def test_failed_publication_never_unlinks_same_name_file_replacement(tmp_path, limits, monkeypatch):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    root = tmp_path / "published"; root.mkdir()
    victim = {"path": None}
    os_module = __import__("os")
    original_unlink = os_module.unlink

    monkeypatch.setattr("ocr_mcp_server.services.merge_publication._publish_stage_anchored", lambda *_: (_ for _ in ()).throw(OSError("injected")))

    def replace_validated_file_before_unlink(path, *args, **kwargs):
        if victim["path"] is None:
            stage = next(root.glob(".merge-stage-*"))
            candidate_path = stage / Path(path).name if kwargs.get("dir_fd") is not None else Path(path)
            owned = candidate_path.with_name(candidate_path.name + ".owned")
            candidate_path.rename(owned)
            candidate_path.write_text("victim", encoding="utf-8")
            victim["path"] = candidate_path
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr("ocr_mcp_server.services.merge_publication.os.unlink", replace_validated_file_before_unlink)
    with pytest.raises(MergeFailure):
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=root, output_version=2, timestamp=NOW, limits=limits)
    assert victim["path"] is None or victim["path"].read_text(encoding="utf-8") == "victim"
    assert list(root.glob(".merge-stage-*"))


def test_failed_publication_never_removes_same_name_directory_replacement(tmp_path, limits, monkeypatch):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    root = tmp_path / "published"; root.mkdir()
    victim = {"path": None}
    os_module = __import__("os")
    original_rmdir = os_module.rmdir

    monkeypatch.setattr("ocr_mcp_server.services.merge_publication._publish_stage_anchored", lambda *_: (_ for _ in ()).throw(OSError("injected")))

    def replace_validated_directory_before_rmdir(path, *args, **kwargs):
        if victim["path"] is None:
            stage = next(root.glob(".merge-stage-*"))
            owned = stage.with_name(stage.name + "-owned-at-rmdir")
            stage.rename(owned)
            stage.mkdir()
            victim["path"] = stage
        return original_rmdir(path, *args, **kwargs)

    monkeypatch.setattr("ocr_mcp_server.services.merge_publication.os.rmdir", replace_validated_directory_before_rmdir)
    with pytest.raises(MergeFailure):
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=root, output_version=2, timestamp=NOW, limits=limits)
    assert victim["path"] is None or victim["path"].is_dir()
    assert list(root.glob(".merge-stage-*"))


def test_anchored_write_closes_raw_descriptor_when_fdopen_fails(tmp_path, monkeypatch):
    descriptor = __import__("os").open(tmp_path / "raw.tmp", __import__("os").O_WRONLY | __import__("os").O_CREAT)
    monkeypatch.setattr("ocr_mcp_server.services.merge_publication.os.open", lambda *args, **kwargs: descriptor)
    monkeypatch.setattr("ocr_mcp_server.services.merge_publication.os.fdopen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected")))
    with pytest.raises(OSError):
        _write_file_anchored(tmp_path, 123, "artifact.json", b"value")
    with pytest.raises(OSError):
        __import__("os").fstat(descriptor)


def test_symlink_publication_root_is_rejected(tmp_path, limits):
    if not hasattr(Path, "symlink_to"):
        pytest.skip("symlinks unavailable")
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    real = tmp_path / "real"; real.mkdir(); link = tmp_path / "link"
    try: link.symlink_to(real, target_is_directory=True)
    except OSError: pytest.skip("symlink privilege unavailable")
    with pytest.raises(MergeFailure):
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=link, output_version=2, timestamp=NOW, limits=limits)


def test_publication_root_below_symlinked_parent_is_rejected(tmp_path, limits):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    real = tmp_path / "real-parent"; real.mkdir(); link = tmp_path / "linked-parent"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(MergeFailure) as raised:
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=link / "child", output_version=2, timestamp=NOW, limits=limits)
    assert raised.value.code == MergeErrorCode.UNSAFE_PUBLICATION_PATH.value
    assert not (real / "child" / "version-00000002").exists()


def test_symlink_version_target_is_unsafe_not_an_idempotent_conflict(tmp_path, limits):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    root = tmp_path / "published"; root.mkdir()
    elsewhere = tmp_path / "elsewhere"; elsewhere.mkdir()
    target = root / "version-00000002"
    try:
        target.symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(MergeFailure) as raised:
        merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=root, output_version=2, timestamp=NOW, limits=limits)
    assert raised.value.code == MergeErrorCode.UNSAFE_PUBLICATION_PATH.value


def test_rollback_publishes_new_version_equal_to_original_and_detects_tampering(tmp_path, limits):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    root = tmp_path / "published"
    merged = merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=root, output_version=2, timestamp=NOW, limits=limits)
    rollback = rollback_publication(merged, publication_root=root, output_version=3, timestamp=NOW, limits=limits)
    assert rollback.manifest_path.read_bytes() == merged.original_snapshot_path.read_bytes()
    assert merged.manifest_path.exists()
    again = rollback_publication(merged, publication_root=root, output_version=3, timestamp=NOW, limits=limits)
    assert again.manifest_sha256 == rollback.manifest_sha256
    merged.original_snapshot_path.write_text("[]", encoding="utf-8")
    with pytest.raises(MergeFailure) as raised:
        rollback_publication(merged, publication_root=root, output_version=4, timestamp=NOW, limits=limits)
    assert raised.value.code == MergeErrorCode.ROLLBACK_VERIFICATION_FAILED.value


def test_rollback_rejects_tampered_final_manifest_and_conflicting_retry(tmp_path, limits):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    root = tmp_path / "published"
    merged = merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=root, output_version=2, timestamp=NOW, limits=limits)
    rollback = rollback_publication(merged, publication_root=root, output_version=3, timestamp=NOW, limits=limits)
    rollback.audit_path.write_text("{}", encoding="utf-8")
    with pytest.raises(MergeFailure) as conflict:
        rollback_publication(merged, publication_root=root, output_version=3, timestamp=NOW, limits=limits)
    assert conflict.value.code == MergeErrorCode.PUBLICATION_CONFLICT.value
    merged.manifest_path.write_text("[]", encoding="utf-8")
    with pytest.raises(MergeFailure) as tampered:
        rollback_publication(merged, publication_root=root, output_version=4, timestamp=NOW, limits=limits)
    assert tampered.value.code == MergeErrorCode.ROLLBACK_VERIFICATION_FAILED.value


def test_rollback_rejects_hash_adjusted_invalid_publication_schema(tmp_path, limits):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    root = tmp_path / "published"
    merged = merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=root, output_version=2, timestamp=NOW, limits=limits)
    invalid = b"{}\n"
    merged.manifest_path.write_bytes(invalid)
    forged = replace(merged, manifest_sha256=sha256(invalid).hexdigest())
    with pytest.raises(MergeFailure) as raised:
        rollback_publication(forged, publication_root=root, output_version=3, timestamp=NOW, limits=limits)
    assert raised.value.code == MergeErrorCode.ROLLBACK_VERIFICATION_FAILED.value


def test_rollback_rejects_publication_manifest_with_extra_top_level_field(tmp_path, limits):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    root = tmp_path / "published"
    merged = merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=root, output_version=2, timestamp=NOW, limits=limits)
    metadata_path = merged.publication_directory / "publication_manifest.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["unexpected"] = "field"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    with pytest.raises(MergeFailure) as raised:
        rollback_publication(merged, publication_root=root, output_version=3, timestamp=NOW, limits=limits)
    assert raised.value.code == MergeErrorCode.ROLLBACK_VERIFICATION_FAILED.value


def test_rollback_rejects_alternate_same_directory_snapshot_even_with_matching_hash(tmp_path, limits):
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    root = tmp_path / "published"
    merged = merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=root, output_version=2, timestamp=NOW, limits=limits)
    alternate = merged.publication_directory / "alternate.json"
    alternate.write_bytes(b"[]\n")
    forged = replace(merged, original_snapshot_path=alternate, original_sha256=sha256(b"[]\n").hexdigest())
    with pytest.raises(MergeFailure) as raised:
        rollback_publication(forged, publication_root=root, output_version=3, timestamp=NOW, limits=limits)
    assert raised.value.code == MergeErrorCode.ROLLBACK_VERIFICATION_FAILED.value


def test_rollback_discards_unsafe_path_conversion_error(tmp_path, limits):
    planted = "SECRET_PATH_CONVERSION"
    node = {"type": "image", "content": {"image_source": {"path": "images/a.png"}}}
    result, collection, candidate, _ = _setup(tmp_path, [node])
    root = tmp_path / "published"
    merged = merge_and_publish(result, collection, {candidate.candidate_id: _ocr()}, publication_root=root, output_version=2, timestamp=NOW, limits=limits)

    class HostilePath:
        def __fspath__(self): raise RuntimeError(planted)

    with pytest.raises(MergeFailure) as raised:
        rollback_publication(merged, publication_root=HostilePath(), output_version=3, timestamp=NOW, limits=limits)
    assert planted not in str(raised.value)
    assert raised.value.__cause__ is None
