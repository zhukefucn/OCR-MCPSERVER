from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import zipfile

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text

from ocr_mcp_server.domain import (
    ArtifactErrorCode,
    ArtifactFailure,
    MergePublicationResult,
    MinerUDocumentResult,
    OrthogonalAngle,
    ReplacementAuditRecord,
    ReplacementDecision,
    ReplacementReason,
    SecondaryOCREngine,
    SecondaryResultKind,
    replacement_audit_metadata_sha256,
    validate_content_free_model_versions,
)
from ocr_mcp_server.infra.artifact_repository import ArtifactRepository
from ocr_mcp_server.infra.retention_repository import RetentionRepository
from ocr_mcp_server.domain.errors import RetentionFailure
from ocr_mcp_server.domain.retention import ContentWriteGuard
from ocr_mcp_server.infra.database import (
    create_database_engine,
    create_session_factory,
    initialize_schema,
)
from ocr_mcp_server.services.artifacts import (
    ArtifactBundler,
    ArtifactLimits,
    ArtifactPackagingStep,
    render_markdown,
)
from ocr_mcp_server.services.retention import RetentionService
from ocr_mcp_server.services.file_storage import FileStorage
from ocr_mcp_server.settings import ArtifactSettings


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)
BATCH_ID = "00000000-0000-4000-8000-000000000009"


class _MarkerRegistry:
    async def bind_empty_lock_marker(self, *_args, initialize, **_kwargs):
        initialize()

    async def bind_lock_marker(self, *_args, **_kwargs):
        return None


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def _audit_record(
    original_node: dict | None = None,
    replacement_node: dict | None = None,
) -> ReplacementAuditRecord:
    original_node = original_node or {
        "type": "image",
        "content": {"image_source": {"path": "images/used.png"}},
    }
    replacement_node = replacement_node or {
        "type": "table",
        "content": {
            "html": "<table><tr><td>值</td></tr></table>",
            "image_source": {"path": "images/used.png"},
        },
    }
    image_sha256 = sha256(b"used-image").hexdigest()
    candidate_id = "candidate-" + sha256(
        ("image-candidate\0file-a\0" f"1\0{image_sha256}").encode()
    ).hexdigest()
    processing_record_id = "secondary-" + sha256(
        ("secondary-record\0file-a\0" f"1\0{image_sha256}").encode()
    ).hexdigest()
    audit_id = "audit-" + sha256(
        (
            "merge-audit\0file-a\0"
            f"{1}\0{2}\0{candidate_id}\0{0}\0{ReplacementReason.REPLACED_TABLE.value}"
        ).encode()
    ).hexdigest()
    return ReplacementAuditRecord(
        audit_id=audit_id,
        task_id="file-a",
        source_version=1,
        output_version=2,
        candidate_id=candidate_id,
        processing_record_id=processing_record_id,
        image_sha256=image_sha256,
        json_pointer="/0/1",
        page_index=0,
        node_index=1,
        original_node_type="image",
        kind=SecondaryResultKind.TABLE,
        angle=OrthogonalAngle.DEG_90,
        confidence=0.9,
        engine=SecondaryOCREngine.PP_STRUCTURE_V3,
        model_versions={"pipeline": "v1"},
        decision=ReplacementDecision.REPLACED,
        reason=ReplacementReason.REPLACED_TABLE,
        timestamp=NOW,
        original_node_snapshot=json.dumps(
            original_node, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        replacement_node_snapshot=json.dumps(
            replacement_node, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    )


def _audit_document(record: ReplacementAuditRecord) -> dict:
    return {
        "task_id": "file-a",
        "source_version": 1,
        "output_version": 2,
        "records": [{
            "audit_id": record.audit_id,
            "task_id": record.task_id,
            "source_version": record.source_version,
            "output_version": record.output_version,
            "candidate_id": record.candidate_id,
            "processing_record_id": record.processing_record_id,
            "image_sha256": record.image_sha256,
            "json_pointer": record.json_pointer,
            "page_index": record.page_index,
            "node_index": record.node_index,
            "original_node_type": record.original_node_type,
            "kind": record.kind.value,
            "angle": int(record.angle),
            "confidence": record.confidence,
            "engine": record.engine.value,
            "model_versions": dict(record.model_versions),
            "decision": record.decision.value,
            "reason": record.reason.value,
            "timestamp": record.timestamp.isoformat(),
            "original_node_snapshot": record.original_node_snapshot,
            "replacement_node_snapshot": record.replacement_node_snapshot,
        }],
    }


def _inputs(tmp_path: Path):
    source = tmp_path / "mineru"
    images = source / "images"
    images.mkdir(parents=True)
    (images / "used.png").write_bytes(b"used-image")
    (images / "unrelated.png").write_bytes(b"must-not-ship")
    original = [[
        {"type": "title", "content": {"text": "标题"}},
        {"type": "image", "content": {"image_source": {"path": "images/used.png"}}},
        {"type": "mystery", "content": {"text": "never render"}},
    ]]
    final = [[
        {"type": "title", "content": {"text": "标题"}},
        {"type": "table", "content": {
            "html": "<table><tr><td>值</td></tr></table>",
            "image_source": {"path": "images/used.png"},
        }},
        {"type": "mystery", "content": {"text": "never render"}},
    ]]
    source_manifest = source / "doc_content_list_v2.json"
    source_manifest.write_bytes(_canonical(original))
    markdown = source / "doc.md"
    markdown.write_text("ORIGINAL MUST NOT DRIVE FINAL", encoding="utf-8")
    middle = source / "doc_middle.json"
    middle.write_bytes(b'{"middle":true}\n')
    legacy = source / "doc_content_list.json"
    legacy.write_bytes(b'[{"legacy":true}]\n')
    result = MinerUDocumentResult(
        file_task_id="file-a",
        upstream_task_id="upstream-a",
        result_root=source,
        markdown_path=markdown,
        middle_json_path=middle,
        content_list_v2_path=source_manifest,
        legacy_content_list_path=legacy,
        images_directory=images,
    )

    publication_dir = tmp_path / "publication" / "version-00000002"
    publication_dir.mkdir(parents=True)
    original_bytes = _canonical(original)
    final_bytes = _canonical(final)
    record = _audit_record(original[0][1], final[0][1])
    audit_bytes = _canonical(_audit_document(record))
    files = {
        "original_content_list_v2.json": original_bytes,
        "content_list_v2.json": final_bytes,
        "secondary_ocr_audit.json": audit_bytes,
    }
    for name, value in files.items():
        (publication_dir / name).write_bytes(value)
    binding = {
        "artifact_kind": "merge",
        "task_id": "file-a",
        "source_version": 1,
        "output_version": 2,
        "files": {name: sha256(value).hexdigest() for name, value in sorted(files.items())},
    }
    (publication_dir / "publication_manifest.json").write_bytes(_canonical(binding))
    publication = MergePublicationResult(
        task_id="file-a",
        source_version=1,
        output_version=2,
        publication_directory=publication_dir,
        original_snapshot_path=publication_dir / "original_content_list_v2.json",
        manifest_path=publication_dir / "content_list_v2.json",
        audit_path=publication_dir / "secondary_ocr_audit.json",
        records=(record,),
        replacement_count=1,
        retained_count=0,
        original_sha256=sha256(original_bytes).hexdigest(),
        manifest_sha256=sha256(final_bytes).hexdigest(),
        audit_sha256=sha256(audit_bytes).hexdigest(),
    )
    return result, publication, files, record


def _replace_task7_file(
    publication: MergePublicationResult,
    name: str,
    content: bytes,
) -> MergePublicationResult:
    path = publication.publication_directory / name
    path.write_bytes(content)
    binding_path = publication.publication_directory / "publication_manifest.json"
    binding = json.loads(binding_path.read_bytes())
    binding["files"][name] = sha256(content).hexdigest()
    binding_path.write_bytes(_canonical(binding))
    field = {
        "content_list_v2.json": "manifest_sha256",
        "original_content_list_v2.json": "original_sha256",
        "secondary_ocr_audit.json": "audit_sha256",
    }[name]
    return replace(publication, **{field: sha256(content).hexdigest()})


def test_artifact_settings_have_positive_mvp_defaults_and_reject_booleans() -> None:
    settings = ArtifactSettings()
    assert settings.max_artifact_bytes == 1024**3
    assert settings.max_entry_bytes == 256 * 1024**2
    assert settings.max_entry_count == 20_000
    assert settings.max_markdown_bytes == 256 * 1024**2
    for name in (
        "max_artifact_bytes", "max_entry_bytes", "max_entry_count", "max_markdown_bytes"
    ):
        with pytest.raises(ValueError):
            ArtifactSettings(**{name: False})


@pytest.mark.parametrize(
    "value",
    [
        "C:/Clients/acme.pdf",
        "srv/acme/model",
        r"\\server\share\model",
        "../model",
        "client.pdf",
        "acme.pdf.v1",
        "team:secret",
        "a..b",
        "document text",
    ],
)
def test_content_free_model_version_grammar_rejects_paths_and_content(value: str) -> None:
    with pytest.raises(ValueError):
        validate_content_free_model_versions({"pipeline": value})


@pytest.mark.parametrize(
    "value", ["v1.2.3", "PP-StructureV3", "2026.07-rc1", "abc123def456"]
)
def test_content_free_model_version_grammar_accepts_realistic_tokens(value: str) -> None:
    assert validate_content_free_model_versions({"pipeline_id": value}) == {
        "pipeline_id": value
    }


def test_content_free_model_version_grammar_rejects_filename_keys_and_non_mappings() -> None:
    with pytest.raises(ValueError):
        validate_content_free_model_versions({"client.pdf": "v1"})
    with pytest.raises(ValueError):
        validate_content_free_model_versions([])  # type: ignore[arg-type]


def test_markdown_renderer_is_conservative_deterministic_and_warns_on_unknown() -> None:
    manifest = [
        [
            {"type": "title", "content": {"text": "A *title*"}},
            {"type": "text", "content": {"text": "Unicode 文本 [x]"}},
            {"type": "list", "content": {"text": "one"}},
            {"type": "code", "content": {"code": "x = 1\nprint(x)"}},
            {"type": "image", "content": {"image_source": {"path": "images/a.png"}}},
            {"type": "table", "content": {"html": "<table><tr><td>x</td></tr></table>"}},
            {"type": "equation_interline", "content": {"math_type": "latex", "math_content": r"x_{i}"}},
            {"type": "future", "content": {"text": "omitted"}},
        ],
        [{"type": "text", "content": {"text": "next"}}],
    ]
    rendered = render_markdown(
        manifest,
        image_names={"images/a.png": "images/000000.png"},
        max_bytes=10_000,
    )
    expected = (
        "# A \\*title\\*\n\n"
        "Unicode 文本 \\[x\\]\n\n"
        "- one\n\n"
        "    x = 1\n    print(x)\n\n"
        "![](images/000000.png)\n\n"
        "<table><tr><td>x</td></tr></table>\n\n"
        "$$\nx_{i}\n$$\n\n---\n\nnext\n"
    ).encode()
    assert rendered.content == expected
    assert rendered.warning_codes == (ArtifactErrorCode.UNSUPPORTED_NODE.value,)
    assert b"omitted" not in rendered.content


def test_unknown_mapping_without_content_is_omitted_with_warning() -> None:
    rendered = render_markdown([[{"type": "future", "metadata": 1}]], image_names={}, max_bytes=100)
    assert rendered.content == b"\n"
    assert rendered.warning_codes == (ArtifactErrorCode.UNSUPPORTED_NODE.value,)


def test_text_nodes_escape_html_metacharacters() -> None:
    rendered = render_markdown(
        [[{"type": "text", "content": {"text": "<b>A&B</b>"}}]],
        image_names={},
        max_bytes=100,
    )
    assert rendered.content == b"&lt;b&gt;A&amp;B&lt;/b&gt;\n"


@pytest.mark.parametrize(
    "manifest",
    [
        {"not": "pages"},
        [[{"type": "table", "content": {"html": "<script>x</script>"}}]],
        [[{"type": "equation_interline", "content": {"math_type": "latex", "math_content": r"\input{x}"}}]],
        [[{"type": "image", "content": {"image_source": {"path": "../x.png"}}}]],
    ],
)
def test_markdown_renderer_rejects_invalid_late_inputs_without_content(manifest) -> None:
    with pytest.raises(ArtifactFailure) as caught:
        render_markdown(manifest, image_names={}, max_bytes=10_000)
    assert caught.value.code in {
        ArtifactErrorCode.INVALID_INPUT.value,
        ArtifactErrorCode.UNSAFE_IMAGE.value,
    }
    assert caught.value.__cause__ is None
    assert "script" not in str(caught.value)


def test_markdown_limit_uses_utf8_bytes() -> None:
    with pytest.raises(ArtifactFailure) as caught:
        render_markdown(
            [[{"type": "text", "content": {"text": "文文"}}]],
            image_names={},
            max_bytes=5,
        )
    assert caught.value.code == ArtifactErrorCode.LIMIT_EXCEEDED.value


def test_zip_is_deterministic_safe_and_contains_only_verified_explicit_inputs(tmp_path: Path) -> None:
    result, publication, expected, _ = _inputs(tmp_path)
    limits = ArtifactLimits(max_artifact_bytes=1_000_000, max_entry_bytes=100_000, max_entry_count=20, max_markdown_bytes=100_000)
    first = ArtifactBundler(limits).publish(
        result, publication,
        artifact_root=(tmp_path / "artifacts-a").absolute(), batch_id=BATCH_ID,
        created_at=NOW, expires_at=NOW + timedelta(hours=24),
    )
    second = ArtifactBundler(limits).publish(
        result, publication,
        artifact_root=(tmp_path / "artifacts-b").absolute(), batch_id=BATCH_ID,
        created_at=NOW, expires_at=NOW + timedelta(hours=24),
    )
    assert first.sha256 == second.sha256
    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.storage_key == second.storage_key
    assert first.path.parent == (tmp_path / "artifacts-a" / BATCH_ID).absolute()
    assert first.storage_key == f"{BATCH_ID}/{first.artifact_id}.zip"
    assert not list((tmp_path / "artifacts-a").glob("artifact-*.zip"))
    with zipfile.ZipFile(first.path) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        assert names == [
            "final.md", "content_list_v2.json", "original_content_list_v2.json",
            "secondary_ocr_audit.json", "mineru/original.md", "mineru/middle.json",
            "mineru/legacy_content_list.json", "images/000000.png", "artifact_manifest.json",
        ]
        assert len(names) == len({name.casefold() for name in names})
        assert all(not name.startswith("/") and ".." not in name.split("/") for name in names)
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in infos)
        assert all(item.create_system == 3 and item.external_attr == (0o100600 << 16) for item in infos)
        assert all(item.flag_bits & 0x08 == 0 for item in infos)
        assert archive.read("content_list_v2.json") == expected["content_list_v2.json"]
        assert archive.read("original_content_list_v2.json") == expected["original_content_list_v2.json"]
        assert archive.read("secondary_ocr_audit.json") == expected["secondary_ocr_audit.json"]
        assert archive.read("images/000000.png") == b"used-image"
        assert b"ORIGINAL MUST NOT DRIVE FINAL" not in archive.read("final.md")
        assert b"unrelated" not in b"".join(archive.read(name) for name in names)
        manifest = json.loads(archive.read("artifact_manifest.json"))
        assert manifest["warning_codes"] == [ArtifactErrorCode.UNSUPPORTED_NODE.value]
        assert manifest["archive_sha256_binding"] == "sqlite_artifact_index"
        assert [entry["name"] for entry in manifest["entries"]] == names[:-1]


def test_zip_rejects_noncanonical_batch_identity_before_creating_artifact_root(
    tmp_path: Path,
) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    artifact_root = (tmp_path / "artifacts").absolute()
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(
            ArtifactLimits(1_000_000, 100_000, 20, 100_000)
        ).publish(
            result,
            publication,
            artifact_root=artifact_root,
            batch_id="batch-a",
            created_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.INVALID_INPUT.value
    assert not artifact_root.exists()


def test_zip_retry_reconciles_identical_and_rejects_tamper_and_conflict(tmp_path: Path) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    bundler = ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000))
    kwargs = dict(artifact_root=(tmp_path / "artifacts").absolute(), batch_id=BATCH_ID, created_at=NOW, expires_at=NOW + timedelta(hours=24))
    first = bundler.publish(result, publication, **kwargs)
    assert bundler.publish(result, publication, **kwargs) == first
    publication.manifest_path.write_bytes(b"planted document content")
    with pytest.raises(ArtifactFailure) as caught:
        bundler.publish(result, publication, **kwargs)
    assert caught.value.code == ArtifactErrorCode.INVALID_INPUT.value
    assert "planted" not in str(caught.value)
    publication.manifest_path.write_bytes(_canonical([]))
    first.path.write_bytes(b"different")
    with pytest.raises(ArtifactFailure) as conflict:
        bundler.publish(result, replace(publication, manifest_sha256=sha256(_canonical([])).hexdigest()), **kwargs)
    assert conflict.value.code in {ArtifactErrorCode.INVALID_INPUT.value, ArtifactErrorCode.PUBLISH_CONFLICT.value}


def test_zip_rejects_forged_audit_even_when_caller_updates_hash_binding(tmp_path: Path) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    forged = _canonical({"task_id": "file-a", "source_version": 1, "output_version": 2, "records": []})
    publication.audit_path.write_bytes(forged)
    binding_path = publication.publication_directory / "publication_manifest.json"
    binding = json.loads(binding_path.read_bytes())
    binding["files"]["secondary_ocr_audit.json"] = sha256(forged).hexdigest()
    binding_path.write_bytes(_canonical(binding))
    publication = replace(publication, audit_sha256=sha256(forged).hexdigest())
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result, publication, artifact_root=(tmp_path / "artifacts").absolute(), batch_id=BATCH_ID,
            created_at=NOW, expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.INVALID_INPUT.value


def test_zip_rejects_unaudited_final_title_mutation_with_recomputed_task7_hashes(tmp_path: Path) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    final = json.loads(publication.manifest_path.read_bytes())
    final[0][0]["content"]["text"] = "unaudited client title"
    publication = _replace_task7_file(
        publication, "content_list_v2.json", _canonical(final)
    )
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result, publication, artifact_root=(tmp_path / "artifacts").absolute(), batch_id=BATCH_ID,
            created_at=NOW, expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.INVALID_INPUT.value
    assert "client title" not in str(caught.value)


@pytest.mark.parametrize("case", ["stale_snapshot", "missing", "duplicate", "retained_replacement"])
def test_zip_rejects_inconsistent_or_nonexact_audit_coverage(tmp_path: Path, case: str) -> None:
    result, publication, _, record = _inputs(tmp_path)
    if case == "stale_snapshot":
        records = (replace(record, original_node_snapshot='{"type":"text"}'),)
        replacements, retained = 1, 0
    elif case == "missing":
        records = ()
        replacements, retained = 0, 0
    elif case == "duplicate":
        second_id = "audit-" + sha256(
            (
                "merge-audit\0file-a\0"
                f"{1}\0{2}\0{record.candidate_id}\0{1}\0{record.reason.value}"
            ).encode()
        ).hexdigest()
        records = (record, replace(record, audit_id=second_id))
        replacements, retained = 2, 0
    else:
        records = (
            replace(
                record,
                decision=ReplacementDecision.RETAINED,
                reason=ReplacementReason.OTHER_IMAGE,
            ),
        )
        replacements, retained = 0, 1
    publication = replace(
        publication,
        records=records,
        replacement_count=replacements,
        retained_count=retained,
    )
    audit_bytes = _canonical({
        "task_id": "file-a",
        "source_version": 1,
        "output_version": 2,
        "records": [_audit_document(item)["records"][0] for item in records],
    })
    publication = _replace_task7_file(
        publication, "secondary_ocr_audit.json", audit_bytes
    )
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result, publication, artifact_root=(tmp_path / "artifacts").absolute(), batch_id=BATCH_ID,
            created_at=NOW, expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.INVALID_INPUT.value


def test_zip_rejects_replacement_kind_inconsistent_with_task7_reason(
    tmp_path: Path,
) -> None:
    result, publication, _, record = _inputs(tmp_path)
    planted = replace(record, kind=SecondaryResultKind.FORMULA)
    publication = replace(publication, records=(planted,))
    publication = _replace_task7_file(
        publication,
        "secondary_ocr_audit.json",
        _canonical(_audit_document(planted)),
    )
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result,
            publication,
            artifact_root=(tmp_path / "artifacts").absolute(),
            batch_id=BATCH_ID,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.INVALID_INPUT.value


@pytest.mark.parametrize(
    "reason",
    [ReplacementReason.ALREADY_STRUCTURED, ReplacementReason.STALE_REFERENCE],
)
def test_zip_rejects_impossible_retained_task7_reason(
    tmp_path: Path, reason: ReplacementReason
) -> None:
    result, publication, _, record = _inputs(tmp_path)
    planted = replace(
        record,
        audit_id="audit-"
        + sha256(
            (
                "merge-audit\0file-a\0"
                f"{1}\0{2}\0{record.candidate_id}\0{0}\0{reason.value}"
            ).encode()
        ).hexdigest(),
        decision=ReplacementDecision.RETAINED,
        reason=reason,
        replacement_node_snapshot=None,
    )
    publication = replace(
        publication,
        records=(planted,),
        replacement_count=0,
        retained_count=1,
    )
    publication = _replace_task7_file(
        publication,
        "content_list_v2.json",
        publication.original_snapshot_path.read_bytes(),
    )
    publication = _replace_task7_file(
        publication,
        "secondary_ocr_audit.json",
        _canonical(_audit_document(planted)),
    )
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result,
            publication,
            artifact_root=(tmp_path / "artifacts").absolute(),
            batch_id=BATCH_ID,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.INVALID_INPUT.value


def test_zip_rejects_pointerless_nonstandalone_audit(tmp_path: Path) -> None:
    result, publication, _, record = _inputs(tmp_path)
    extra = replace(
        record,
        audit_id="audit-"
        + sha256(
            (
                "merge-audit\0file-a\0"
                f"{1}\0{2}\0{record.candidate_id}\0{1}\0"
                f"{ReplacementReason.OTHER_IMAGE.value}"
            ).encode()
        ).hexdigest(),
        json_pointer=None,
        page_index=None,
        node_index=None,
        original_node_type=None,
        kind=SecondaryResultKind.OTHER,
        decision=ReplacementDecision.RETAINED,
        reason=ReplacementReason.OTHER_IMAGE,
        replacement_node_snapshot=None,
    )
    records = (record, extra)
    publication = replace(
        publication,
        records=records,
        replacement_count=1,
        retained_count=1,
    )
    publication = _replace_task7_file(
        publication,
        "secondary_ocr_audit.json",
        _canonical(
            {
                "task_id": "file-a",
                "source_version": 1,
                "output_version": 2,
                "records": [
                    _audit_document(item)["records"][0] for item in records
                ],
            }
        ),
    )
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result,
            publication,
            artifact_root=(tmp_path / "artifacts").absolute(),
            batch_id=BATCH_ID,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.INVALID_INPUT.value


def test_zip_rejects_unsafe_model_identifier_before_manifest_write(tmp_path: Path) -> None:
    result, publication, _, record = _inputs(tmp_path)
    planted = replace(record, model_versions={"pipeline": "C:/Clients/acme.pdf"})
    publication = replace(publication, records=(planted,))
    publication = _replace_task7_file(
        publication,
        "secondary_ocr_audit.json",
        _canonical(_audit_document(planted)),
    )
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result, publication, artifact_root=(tmp_path / "artifacts").absolute(), batch_id=BATCH_ID,
            created_at=NOW, expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.INVALID_INPUT.value
    assert "acme.pdf" not in str(caught.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows stage lifecycle contract")
def test_windows_cleanup_capability_failure_aborts_before_publication(
    tmp_path: Path, monkeypatch
) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    artifact_root = (tmp_path / "artifacts").absolute()
    from ocr_mcp_server.services import artifacts as module

    def unavailable(*_args, **_kwargs):
        raise OSError("planted cleanup capability failure")

    monkeypatch.setattr(module._PinnedArtifactRoot, "arm_stage_cleanup", unavailable)
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result,
            publication,
            artifact_root=artifact_root,
            batch_id=BATCH_ID,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.PUBLISH_FAILED.value
    assert not list(artifact_root.rglob("artifact-*.zip"))
    assert not list(artifact_root.rglob(".artifact-stage-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows stage lifecycle contract")
def test_windows_native_cleanup_failure_uses_handle_bound_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    artifact_root = (tmp_path / "artifacts").absolute()
    from ocr_mcp_server.services import artifacts as module

    def unavailable(*_args, **_kwargs):
        raise OSError("planted native disposition failure")

    monkeypatch.setattr(module._PinnedArtifactRoot, "_set_stage_cleanup", unavailable)
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result,
            publication,
            artifact_root=artifact_root,
            batch_id=BATCH_ID,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.PUBLISH_FAILED.value
    assert not list(artifact_root.rglob("artifact-*.zip"))
    assert not list(artifact_root.rglob(".artifact-stage-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows stage lifecycle contract")
def test_windows_double_cleanup_api_failure_scrubs_unpublished_stage(
    tmp_path: Path, monkeypatch
) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    artifact_root = (tmp_path / "artifacts").absolute()
    from ocr_mcp_server.services import artifacts as module

    def unavailable(*_args, **_kwargs):
        raise OSError("planted disposition failure")

    monkeypatch.setattr(module._PinnedArtifactRoot, "_set_stage_cleanup", unavailable)
    monkeypatch.setattr(
        module._PinnedArtifactRoot, "_set_stage_cleanup_extended", unavailable
    )

    artifact_root.mkdir(parents=True)
    replacement = artifact_root / "attacker-replacement.bin"
    replacement.write_bytes(b"must survive")

    def forbidden_unlink(*_args, **_kwargs):
        raise AssertionError("path cleanup must not run after handle cleanup fails")

    monkeypatch.setattr(module.os, "unlink", forbidden_unlink)
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result,
            publication,
            artifact_root=artifact_root,
            batch_id=BATCH_ID,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.PUBLISH_FAILED.value
    assert not list(artifact_root.rglob("artifact-*.zip"))
    stages = list((artifact_root / BATCH_ID).glob(".artifact-stage-*"))
    assert len(stages) == 1
    assert stages[0].read_bytes() == b""
    assert replacement.read_bytes() == b"must survive"


@pytest.mark.skipif(os.name != "nt", reason="Windows stage lifecycle contract")
def test_windows_truncate_failure_uses_handle_scrub_and_always_closes(
    tmp_path: Path, monkeypatch
) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    artifact_root = (tmp_path / "artifacts").absolute()
    from ocr_mcp_server.services import artifacts as module

    def unavailable(*_args, **_kwargs):
        raise OSError("planted disposition failure")

    attempted_descriptors: list[int] = []

    def unavailable_truncate(descriptor: int, _size: int):
        attempted_descriptors.append(descriptor)
        raise OSError("planted ftruncate failure")

    monkeypatch.setattr(module._PinnedArtifactRoot, "_set_stage_cleanup", unavailable)
    monkeypatch.setattr(
        module._PinnedArtifactRoot, "_set_stage_cleanup_extended", unavailable
    )
    monkeypatch.setattr(module.os, "ftruncate", unavailable_truncate)
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result,
            publication,
            artifact_root=artifact_root,
            batch_id=BATCH_ID,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.PUBLISH_FAILED.value
    assert len(attempted_descriptors) == 1
    descriptor = attempted_descriptors[0]
    try:
        with pytest.raises(OSError):
            os.fstat(descriptor)
        stages = list((artifact_root / BATCH_ID).glob(".artifact-stage-*"))
        assert len(stages) == 1
        assert stages[0].read_bytes() == b""
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def test_publish_interruption_leaves_no_visible_target_or_stage(tmp_path: Path, monkeypatch) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    artifact_root = (tmp_path / "artifacts").absolute()
    from ocr_mcp_server.services import artifacts as module

    def interrupted(*_args, **_kwargs):
        raise OSError("planted path and content")

    monkeypatch.setattr(module._PinnedArtifactRoot, "link_no_replace", interrupted)
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result, publication, artifact_root=artifact_root, batch_id=BATCH_ID,
            created_at=NOW, expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.PUBLISH_FAILED.value
    assert not list(artifact_root.rglob("*.zip"))
    assert not list(artifact_root.rglob(".artifact-stage-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX anonymous publication contract")
def test_posix_publication_falls_back_when_empty_path_link_is_restricted(
    tmp_path: Path, monkeypatch
) -> None:
    from ocr_mcp_server.services import artifacts as module

    root = tmp_path / "artifacts"
    root.mkdir()
    real_libc = module.ctypes.CDLL(None, use_errno=True)
    real_linkat = real_libc.linkat
    real_linkat.argtypes = (
        module.ctypes.c_int,
        module.ctypes.c_char_p,
        module.ctypes.c_int,
        module.ctypes.c_char_p,
        module.ctypes.c_int,
    )
    real_linkat.restype = module.ctypes.c_int
    calls: list[tuple[bytes, int]] = []

    class RestrictedLinkAt:
        argtypes = None
        restype = None

        def __call__(self, source_fd, source_name, root_fd, target_name, flags):
            calls.append((source_name, flags))
            if source_name == b"":
                module.ctypes.set_errno(errno.ENOENT)
                return -1
            return real_linkat(source_fd, source_name, root_fd, target_name, flags)

    class RestrictedLibC:
        linkat = RestrictedLinkAt()

    monkeypatch.setattr(
        module.ctypes, "CDLL", lambda *_args, **_kwargs: RestrictedLibC()
    )
    with module._PinnedArtifactRoot(root, os.lstat(root)) as pinned:
        descriptor, stage_name, _stage_path = pinned.create_stage()
        try:
            os.write(descriptor, b"descriptor-bound-stage")
            os.fsync(descriptor)
            pinned.link_no_replace(descriptor, stage_name, "published.zip")
            assert module._same_object(
                os.fstat(descriptor),
                os.stat(
                    "published.zip",
                    dir_fd=pinned.descriptor,
                    follow_symlinks=False,
                ),
            )
        finally:
            os.close(descriptor)
    assert calls[0] == (b"", 0x1000)
    assert calls[1][0].startswith(b"/proc/self/fd/") and calls[1][1] == 0x400
    assert not list(root.glob(".artifact-stage-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX named publication fallback")
def test_posix_publication_falls_back_when_otmpfile_is_unsupported(
    tmp_path: Path, monkeypatch
) -> None:
    from ocr_mcp_server.services import artifacts as module

    root = tmp_path / "artifacts"
    root.mkdir()
    real_open = module.os.open
    temporary_flag = getattr(module.os, "O_TMPFILE", 0)
    assert temporary_flag

    def without_otmpfile(path, flags, *args, **kwargs):
        if flags & temporary_flag == temporary_flag:
            raise OSError(errno.EOPNOTSUPP, "planted unsupported anonymous stage")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", without_otmpfile)
    with module._PinnedArtifactRoot(root, os.lstat(root)) as pinned:
        descriptor, stage_name, _stage_path = pinned.create_stage()
        try:
            assert stage_name is not None
            os.write(descriptor, b"descriptor-bound-stage")
            os.fsync(descriptor)
            pinned.link_no_replace(descriptor, stage_name, "published.zip")
            assert module._same_object(
                os.fstat(descriptor),
                os.stat(
                    "published.zip",
                    dir_fd=pinned.descriptor,
                    follow_symlinks=False,
                ),
            )
        finally:
            os.close(descriptor)
    assert not list(root.glob(".artifact-stage-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX named publication fallback")
def test_posix_named_stage_cleanup_removes_only_the_owned_name(
    tmp_path: Path, monkeypatch
) -> None:
    from ocr_mcp_server.services import artifacts as module

    root = tmp_path / "artifacts"
    root.mkdir()
    real_open = module.os.open
    temporary_flag = getattr(module.os, "O_TMPFILE", 0)

    def without_otmpfile(path, flags, *args, **kwargs):
        if temporary_flag and flags & temporary_flag == temporary_flag:
            raise OSError(errno.EOPNOTSUPP, "planted unsupported anonymous stage")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", without_otmpfile)
    with module._PinnedArtifactRoot(root, os.lstat(root)) as pinned:
        descriptor, stage_name, _stage_path = pinned.create_stage()
        assert stage_name is not None
        try:
            os.write(descriptor, b"verified stage content")
            os.fsync(descriptor)
            pinned.force_stage_cleanup(descriptor, stage_name)
            assert os.fstat(descriptor).st_size == 0
            assert not (root / stage_name).exists()
        finally:
            os.close(descriptor)


@pytest.mark.skipif(os.name == "nt", reason="POSIX named publication fallback")
def test_posix_named_publication_does_not_require_renameat2(
    tmp_path: Path, monkeypatch
) -> None:
    from ocr_mcp_server.services import artifacts as module

    root = tmp_path / "artifacts"
    root.mkdir()
    real_open = module.os.open
    temporary_flag = getattr(module.os, "O_TMPFILE", 0)
    real_libc = module.ctypes.CDLL(None, use_errno=True)
    real_linkat = real_libc.linkat
    real_linkat.argtypes = (
        module.ctypes.c_int,
        module.ctypes.c_char_p,
        module.ctypes.c_int,
        module.ctypes.c_char_p,
        module.ctypes.c_int,
    )
    real_linkat.restype = module.ctypes.c_int
    calls: list[tuple[bytes, int]] = []

    def without_otmpfile(path, flags, *args, **kwargs):
        if temporary_flag and flags & temporary_flag == temporary_flag:
            raise OSError(errno.EOPNOTSUPP, "planted unsupported anonymous stage")
        return real_open(path, flags, *args, **kwargs)

    class RestrictedLinkAt:
        argtypes = None
        restype = None

        def __call__(self, source_fd, source_name, root_fd, target_name, flags):
            calls.append((source_name, flags))
            if source_name == b"":
                module.ctypes.set_errno(errno.ENOENT)
                return -1
            return real_linkat(source_fd, source_name, root_fd, target_name, flags)

    class LibCWithoutRenameAt2:
        linkat = RestrictedLinkAt()

    monkeypatch.setattr(module.os, "open", without_otmpfile)
    monkeypatch.setattr(
        module.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: LibCWithoutRenameAt2(),
    )
    with module._PinnedArtifactRoot(root, os.lstat(root)) as pinned:
        descriptor, stage_name, _stage_path = pinned.create_stage()
        assert stage_name is not None
        try:
            os.write(descriptor, b"descriptor-bound-stage")
            os.fsync(descriptor)
            pinned.link_no_replace(descriptor, stage_name, "published.zip")
            assert module._same_object(
                os.fstat(descriptor),
                os.stat(
                    "published.zip",
                    dir_fd=pinned.descriptor,
                    follow_symlinks=False,
                ),
            )
        finally:
            os.close(descriptor)
    assert calls[0] == (b"", 0x1000)
    assert calls[1][0].startswith(b"/proc/self/fd/") and calls[1][1] == 0x400
    assert (root / "published.zip").read_bytes() == b"descriptor-bound-stage"
    assert not list(root.glob(".artifact-stage-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX named publication fallback")
def test_posix_named_stage_swap_never_succeeds_or_leaks_stage_content(
    tmp_path: Path, monkeypatch
) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    artifact_root = (tmp_path / "artifacts").absolute()
    from ocr_mcp_server.services import artifacts as module

    real_open = module.os.open
    temporary_flag = getattr(module.os, "O_TMPFILE", 0)

    def without_otmpfile(path, flags, *args, **kwargs):
        if temporary_flag and flags & temporary_flag == temporary_flag:
            raise OSError(errno.EOPNOTSUPP, "planted unsupported anonymous stage")
        return real_open(path, flags, *args, **kwargs)

    moved_stage: Path | None = None
    replacement_target: Path | None = None

    def swap_before_publish(self, _descriptor, stage_name, target_name):
        nonlocal moved_stage, replacement_target
        assert stage_name is not None and self.descriptor is not None
        moved_stage = self.path / ".attacker-moved-stage"
        replacement_target = self.path / target_name
        os.rename(
            stage_name,
            moved_stage.name,
            src_dir_fd=self.descriptor,
            dst_dir_fd=self.descriptor,
        )
        replacement_target.write_bytes(b"attacker replacement")

    monkeypatch.setattr(module.os, "open", without_otmpfile)
    monkeypatch.setattr(module._PinnedArtifactRoot, "link_no_replace", swap_before_publish)
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result,
            publication,
            artifact_root=artifact_root,
            batch_id=BATCH_ID,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.PUBLISH_FAILED.value
    assert moved_stage is not None and moved_stage.read_bytes() == b""
    assert replacement_target is not None
    assert replacement_target.read_bytes() == b"attacker replacement"


@pytest.mark.skipif(os.name == "nt", reason="POSIX named publication fallback")
def test_posix_named_target_late_swap_never_returns_success(
    tmp_path: Path, monkeypatch
) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    artifact_root = (tmp_path / "artifacts").absolute()
    from ocr_mcp_server.services import artifacts as module

    real_open = module.os.open
    temporary_flag = getattr(module.os, "O_TMPFILE", 0)

    def without_otmpfile(path, flags, *args, **kwargs):
        if temporary_flag and flags & temporary_flag == temporary_flag:
            raise OSError(errno.EOPNOTSUPP, "planted unsupported anonymous stage")
        return real_open(path, flags, *args, **kwargs)

    original_fsync = module._PinnedArtifactRoot.fsync
    moved_target: Path | None = None
    replacement_target: Path | None = None

    def swap_after_first_identity_check(self):
        nonlocal moved_target, replacement_target
        target = next(self.path.glob("artifact-*.zip"))
        moved_target = self.path / ".attacker-late-moved"
        replacement_target = target
        os.rename(target, moved_target)
        replacement_target.write_bytes(b"x" * moved_target.stat().st_size)
        return original_fsync(self)

    monkeypatch.setattr(module.os, "open", without_otmpfile)
    monkeypatch.setattr(module._PinnedArtifactRoot, "fsync", swap_after_first_identity_check)
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result,
            publication,
            artifact_root=artifact_root,
            batch_id=BATCH_ID,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.PUBLISH_FAILED.value
    assert moved_target is not None and moved_target.read_bytes() == b""
    assert replacement_target is not None
    assert set(replacement_target.read_bytes()) == {ord("x")}


def test_exact_retry_same_size_target_swap_never_returns_success(
    tmp_path: Path, monkeypatch
) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    artifact_root = (tmp_path / "artifacts").absolute()
    bundler = ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000))
    kwargs = dict(
        artifact_root=artifact_root,
        batch_id=BATCH_ID,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=24),
    )
    first = bundler.publish(result, publication, **kwargs)
    from ocr_mcp_server.services import artifacts as module

    original_fsync = module._PinnedArtifactRoot.fsync
    moved_target = first.path.with_name(".attacker-moved-exact-target")
    replacement = b"x" * first.size_bytes

    def swap_during_retry_fsync(self):
        os.rename(first.path, moved_target)
        first.path.write_bytes(replacement)
        return original_fsync(self)

    monkeypatch.setattr(module._PinnedArtifactRoot, "fsync", swap_during_retry_fsync)
    with pytest.raises(ArtifactFailure) as caught:
        bundler.publish(result, publication, **kwargs)

    assert caught.value.code == ArtifactErrorCode.PUBLISH_CONFLICT.value
    assert first.path.read_bytes() == replacement
    assert sha256(moved_target.read_bytes()).hexdigest() == first.sha256


def test_exact_retry_same_inode_content_mutation_never_returns_success(
    tmp_path: Path, monkeypatch
) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    artifact_root = (tmp_path / "artifacts").absolute()
    bundler = ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000))
    kwargs = dict(
        artifact_root=artifact_root,
        batch_id=BATCH_ID,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=24),
    )
    first = bundler.publish(result, publication, **kwargs)
    from ocr_mcp_server.services import artifacts as module

    original_fsync = module._PinnedArtifactRoot.fsync
    replacement = b"y" * first.size_bytes

    def mutate_during_retry_fsync(self):
        with first.path.open("r+b") as target:
            target.write(replacement)
            target.flush()
            os.fsync(target.fileno())
        return original_fsync(self)

    monkeypatch.setattr(module._PinnedArtifactRoot, "fsync", mutate_during_retry_fsync)
    with pytest.raises(ArtifactFailure) as caught:
        bundler.publish(result, publication, **kwargs)

    assert caught.value.code == ArtifactErrorCode.PUBLISH_CONFLICT.value
    assert first.path.read_bytes() == replacement


@pytest.mark.skipif(os.name == "nt", reason="POSIX named publication fallback")
def test_posix_named_stage_replacement_on_exact_retry_never_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    artifact_root = (tmp_path / "artifacts").absolute()
    bundler = ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000))
    kwargs = dict(
        artifact_root=artifact_root,
        batch_id=BATCH_ID,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=24),
    )
    first = bundler.publish(result, publication, **kwargs)
    from ocr_mcp_server.services import artifacts as module

    real_open = module.os.open
    temporary_flag = getattr(module.os, "O_TMPFILE", 0)

    def without_otmpfile(path, flags, *args, **kwargs):
        if temporary_flag and flags & temporary_flag == temporary_flag:
            raise OSError(errno.EOPNOTSUPP, "planted unsupported anonymous stage")
        return real_open(path, flags, *args, **kwargs)

    moved_stage: Path | None = None
    replacement_stage: Path | None = None

    def replace_stage_then_report_existing(self, _descriptor, stage_name, _target_name):
        nonlocal moved_stage, replacement_stage
        assert stage_name is not None and self.descriptor is not None
        moved_stage = self.path / ".attacker-moved-retry-stage"
        replacement_stage = self.path / stage_name
        os.rename(
            stage_name,
            moved_stage.name,
            src_dir_fd=self.descriptor,
            dst_dir_fd=self.descriptor,
        )
        replacement_stage.write_bytes(b"attacker stage replacement")
        raise FileExistsError(errno.EEXIST, "planted exact-existing target")

    monkeypatch.setattr(module.os, "open", without_otmpfile)
    monkeypatch.setattr(
        module._PinnedArtifactRoot,
        "link_no_replace",
        replace_stage_then_report_existing,
    )
    with pytest.raises(ArtifactFailure) as caught:
        bundler.publish(result, publication, **kwargs)

    assert caught.value.code == ArtifactErrorCode.PUBLISH_FAILED.value
    assert moved_stage is not None and moved_stage.read_bytes() == b""
    assert replacement_stage is not None
    assert replacement_stage.read_bytes() == b"attacker stage replacement"
    assert sha256(first.path.read_bytes()).hexdigest() == first.sha256


def test_post_link_identity_swap_never_returns_success(tmp_path: Path, monkeypatch) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    artifact_root = (tmp_path / "artifacts").absolute()
    from ocr_mcp_server.services import artifacts as module

    original = module._PinnedArtifactRoot._named_stat

    def replaced_identity(self, name):
        value = original(self, name)
        if name.startswith("artifact-") and name.endswith(".zip"):
            fields = list(value)
            fields[1] += 1
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(module._PinnedArtifactRoot, "_named_stat", replaced_identity)
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result, publication, artifact_root=artifact_root, batch_id=BATCH_ID,
            created_at=NOW, expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.PUBLISH_FAILED.value


def test_zip_rejects_missing_changed_symlinked_image_and_limits(tmp_path: Path) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    root = (tmp_path / "artifacts").absolute()
    image = result.images_directory / "used.png"
    image.unlink()
    with pytest.raises(ArtifactFailure) as missing:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result, publication, artifact_root=root, batch_id=BATCH_ID, created_at=NOW, expires_at=NOW + timedelta(hours=24)
        )
    assert missing.value.code == ArtifactErrorCode.UNSAFE_IMAGE.value
    image.symlink_to(result.images_directory / "unrelated.png")
    with pytest.raises(ArtifactFailure) as linked:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result, publication, artifact_root=root, batch_id=BATCH_ID, created_at=NOW, expires_at=NOW + timedelta(hours=24)
        )
    assert linked.value.code == ArtifactErrorCode.UNSAFE_IMAGE.value
    image.unlink()
    image.write_bytes(b"used-image")
    with pytest.raises(ArtifactFailure) as limited:
        ArtifactBundler(ArtifactLimits(10, 100_000, 20, 100_000)).publish(
            result, publication, artifact_root=root, batch_id=BATCH_ID, created_at=NOW, expires_at=NOW + timedelta(hours=24)
        )
    assert limited.value.code == ArtifactErrorCode.LIMIT_EXCEEDED.value


def test_zip_rejects_referenced_image_changed_after_task7(tmp_path: Path) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    (result.images_directory / "used.png").write_bytes(b"changed-after-task7")
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result, publication, artifact_root=(tmp_path / "artifacts").absolute(), batch_id=BATCH_ID,
            created_at=NOW, expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.UNSAFE_IMAGE.value


def _db_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest_asyncio.fixture
async def artifact_repository(tmp_path: Path):
    engine = create_database_engine(_db_url(tmp_path / "artifacts.sqlite3"))
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    # Artifact rows deliberately bind to Task 8 task identities.
    from ocr_mcp_server.infra.task_repository import TaskRepository
    tasks = TaskRepository(sessions)
    created = await tasks.create_batch("artifact-test", ["file-a"])
    repository = ArtifactRepository(sessions)
    try:
        yield repository, engine, created.batch.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_repository_registers_exact_idempotent_content_free_metadata_and_orders_reads(tmp_path: Path, artifact_repository) -> None:
    repository, engine, batch_id = artifact_repository
    result, publication, _, record = _inputs(tmp_path)
    bundle = ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
        result, publication, artifact_root=(tmp_path / "artifacts").absolute(), batch_id=batch_id,
        created_at=NOW, expires_at=NOW + timedelta(hours=24),
    )
    guard = await repository.acquire_content_write(
        batch_id, "file-a", now=NOW, lease_seconds=60
    )
    first = await repository.register(
        bundle, publication.records, write_guard=guard
    )
    repeated = await repository.register(
        bundle, publication.records, write_guard=guard
    )
    assert first == repeated
    assert first.available is True and first.deleted_at is None and first.version == 1
    assert (await repository.list_for_file("file-a")) == (first,)
    assert (await repository.list_for_batch(batch_id)) == (first,)
    assert (await repository.list_for_batch(batch_id, result_version=2)) == (first,)
    audits = await repository.list_audits_for_file("file-a", output_version=2)
    assert (await repository.list_audits_for_batch(batch_id, output_version=2)) == audits
    assert len(audits) == 1
    audit = audits[0]
    assert audit.audit_id == record.audit_id
    assert not hasattr(audit, "original_node_snapshot")
    assert not hasattr(audit, "replacement_node_snapshot")
    assert dict(audit.model_versions) == {"pipeline": "v1"}
    with pytest.raises(ArtifactFailure) as conflict:
        await repository.register(
            replace(bundle, sha256="0" * 64),
            publication.records,
            write_guard=guard,
        )
    assert conflict.value.code == ArtifactErrorCode.INDEX_CONFLICT.value

    different = replace(
        record,
        audit_id="audit-" + "e" * 64,
        candidate_id="candidate-" + "e" * 64,
        processing_record_id="secondary-" + "e" * 64,
    )
    with pytest.raises(ArtifactFailure) as audit_conflict:
        await repository.register(bundle, (different,), write_guard=guard)
    assert audit_conflict.value.code == ArtifactErrorCode.INDEX_CONFLICT.value

    unsafe_model = replace(record, model_versions={"pipeline": "client name.pdf\n/document text"})
    with pytest.raises(ArtifactFailure) as unsafe_metadata:
        await repository.register(bundle, (unsafe_model,), write_guard=guard)
    assert unsafe_metadata.value.code == ArtifactErrorCode.INDEX_CONFLICT.value
    await repository.release_content_write(guard)

    async with engine.connect() as connection:
        columns = await connection.run_sync(lambda sync: {
            table: {column["name"] for column in inspect(sync).get_columns(table)}
            for table in ("artifacts", "replacement_audit_metadata")
        })
        raw = " ".join(str(row) for row in (await connection.execute(text(
            "SELECT * FROM replacement_audit_metadata"
        ))).all())
    forbidden = {"content", "filename", "path", "snapshot", "json_pointer", "page_index", "node_index"}
    assert not (forbidden & columns["replacement_audit_metadata"])
    assert "not metadata" not in raw
    assert str(tmp_path) not in raw


@pytest.mark.asyncio
async def test_repository_extends_batch_content_retention_to_artifact_expiry(
    tmp_path: Path, artifact_repository
) -> None:
    repository, engine, batch_id = artifact_repository
    result, publication, _, _ = _inputs(tmp_path)
    expires_at = NOW + timedelta(days=365)
    artifact_root = (tmp_path / "artifacts").absolute()
    bundle = ArtifactBundler(
        ArtifactLimits(1_000_000, 100_000, 20, 100_000)
    ).publish(
        result,
        publication,
        artifact_root=artifact_root,
        batch_id=batch_id,
        created_at=NOW,
        expires_at=expires_at,
    )

    guard = await repository.acquire_content_write(
        batch_id, "file-a", now=NOW, lease_seconds=60
    )
    await repository.register(bundle, publication.records, write_guard=guard)
    await repository.release_content_write(guard)

    retention = RetentionRepository(create_session_factory(engine))
    assert (await retention.get(batch_id)).content_due_at == expires_at


@pytest.mark.asyncio
async def test_real_publish_register_and_retention_cleanup_share_the_batch_root(
    tmp_path: Path, artifact_repository
) -> None:
    repository, engine, batch_id = artifact_repository
    sessions = create_session_factory(engine)
    retention = RetentionRepository(sessions)
    artifact_root = (tmp_path / "artifacts").absolute()
    data_root = (tmp_path / "data").absolute()
    result, publication, _, _ = _inputs(tmp_path)
    bundle = ArtifactBundler(
        ArtifactLimits(1_000_000, 100_000, 20, 100_000)
    ).publish(
        result,
        publication,
        artifact_root=artifact_root,
        batch_id=batch_id,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=24),
    )
    guard = await repository.acquire_content_write(
        batch_id, "file-a", now=NOW, lease_seconds=60
    )
    await repository.register(bundle, publication.records, write_guard=guard)
    await repository.release_content_write(guard)
    due = (await retention.get(batch_id)).content_due_at

    cleanup = await RetentionService(
        retention, data_root, artifact_root
    ).run_once("worker", now=due, lease_seconds=60, limit=1)

    assert (cleanup.content_deleted, cleanup.failed) == (1, 0)
    assert bundle.path.exists() is False
    retained_archives = list(artifact_root.rglob("*.zip"))
    assert len(retained_archives) == 1
    assert retained_archives[0].read_bytes() == b""
    assert (await repository.list_for_file("file-a"))[0].available is False


@pytest.mark.asyncio
async def test_early_delete_and_artifact_write_guard_have_atomic_ordering(
    tmp_path: Path, artifact_repository
) -> None:
    repository, engine, batch_id = artifact_repository
    sessions = create_session_factory(engine)
    retention = RetentionRepository(sessions)
    result, publication, _, _ = _inputs(tmp_path)
    artifact_root = (tmp_path / "artifacts").absolute()
    bundle = ArtifactBundler(
        ArtifactLimits(1_000_000, 100_000, 20, 100_000)
    ).publish(
        result,
        publication,
        artifact_root=artifact_root,
        batch_id=batch_id,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=24),
    )

    guard = await repository.acquire_content_write(
        batch_id,
        "file-a",
        now=NOW,
        lease_seconds=60,
    )
    assert await retention.request_early_delete(batch_id, now=NOW) is True
    with pytest.raises(ArtifactFailure) as interrupted_registration:
        await repository.register(bundle, publication.records, write_guard=guard)
    assert interrupted_registration.value.code == ArtifactErrorCode.INDEX_CONFLICT.value
    await repository.release_content_write(guard)

    with pytest.raises(ArtifactFailure) as blocked_registration:
        await repository.register(bundle, publication.records, write_guard=guard)
    assert blocked_registration.value.code == ArtifactErrorCode.INDEX_CONFLICT.value
    with pytest.raises(ArtifactFailure) as blocked_guard:
        await repository.acquire_content_write(
            batch_id,
            "file-a",
            now=NOW + timedelta(seconds=1),
            lease_seconds=60,
        )
    assert blocked_guard.value.code == ArtifactErrorCode.INDEX_CONFLICT.value

    assert await RetentionService(
        retention,
        (tmp_path / "data").absolute(),
        artifact_root,
    ).delete_task(
        batch_id,
        now=NOW + timedelta(seconds=1),
        worker_id="cleanup",
    ) is True
    assert all(path.read_bytes() == b"" for path in artifact_root.rglob("*.zip"))


@pytest.mark.asyncio
async def test_packaging_step_reports_exact_packaging_then_publishing_counters(tmp_path: Path, artifact_repository) -> None:
    repository, engine, batch_id = artifact_repository
    result, publication, _, _ = _inputs(tmp_path)
    bundler = ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000))
    events = []

    class Progress:
        async def report(self, stage, counters=None):
            events.append((stage.value, counters.completed_units, counters.total_units, counters.unit.value))

    class Cancellation:
        def checkpoint(self):
            return None

    guard_events = []

    class GuardedRepository:
        async def acquire_content_write(self, *args, **kwargs):
            guard = await repository.acquire_content_write(*args, **kwargs)
            guard_events.append(("acquire", guard))
            return guard

        async def register(self, bundle, records, *, write_guard):
            guard_events.append(("register", write_guard))
            return await repository.register(
                bundle, records, write_guard=write_guard
            )

        async def release_content_write(self, guard):
            guard_events.append(("release", guard))
            await repository.release_content_write(guard)

    step = ArtifactPackagingStep(
        bundler,
        GuardedRepository(),
        batch_locks=FileStorage((tmp_path / "data").absolute()),
        marker_registry=RetentionRepository(create_session_factory(engine)),
    )
    artifact = await step.run(
        result, publication, artifact_root=(tmp_path / "artifacts").absolute(), batch_id=batch_id,
        created_at=NOW, expires_at=NOW + timedelta(hours=24), progress=Progress(), cancellation=Cancellation(),
    )
    assert events == [
        ("packaging", 0, None, "bytes"),
        ("packaging", artifact.size_bytes, artifact.size_bytes, "bytes"),
        ("publishing", 0, 1, "items"),
        ("publishing", 1, 1, "items"),
    ]
    assert [name for name, _ in guard_events] == ["acquire", "register", "release"]
    assert len({guard.token for _, guard in guard_events}) == 1
    assert artifact == (await repository.list_for_file("file-a"))[0]


@pytest.mark.asyncio
async def test_expired_artifact_guard_cannot_bypass_the_shared_batch_lock(
    tmp_path: Path, artifact_repository
) -> None:
    repository, engine, batch_id = artifact_repository
    retention = RetentionRepository(create_session_factory(engine))
    data_root = (tmp_path / "data").absolute()
    artifact_root = (tmp_path / "artifacts").absolute()
    result, publication, _, _ = _inputs(tmp_path)
    packaging_holds_lock = asyncio.Event()
    resume_packaging = asyncio.Event()
    early_delete_requested = asyncio.Event()

    class Progress:
        async def report(self, stage, counters=None):
            if stage.value == "packaging" and counters.completed_units == 0:
                packaging_holds_lock.set()
                await resume_packaging.wait()

    class Cancellation:
        def checkpoint(self):
            return None

    class ObservedRetention:
        def __getattr__(self, name):
            return getattr(retention, name)

        async def request_early_delete(self, selected_batch_id, *, now):
            result = await retention.request_early_delete(selected_batch_id, now=now)
            early_delete_requested.set()
            return result

    step = ArtifactPackagingStep(
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)),
        repository,
        batch_locks=FileStorage(data_root),
        marker_registry=retention,
        now_factory=lambda: NOW,
        write_lease_seconds=1,
    )
    packaging = asyncio.create_task(
        step.run(
            result,
            publication,
            artifact_root=artifact_root,
            batch_id=batch_id,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=24),
            progress=Progress(),
            cancellation=Cancellation(),
        )
    )
    await packaging_holds_lock.wait()
    cleanup = asyncio.create_task(
        RetentionService(
            ObservedRetention(), data_root, artifact_root
        ).delete_task(
            batch_id,
            now=NOW + timedelta(seconds=1),
            worker_id="cleanup",
        )
    )
    await early_delete_requested.wait()
    await asyncio.sleep(0)
    assert cleanup.done() is False

    resume_packaging.set()
    with pytest.raises(ArtifactFailure):
        await packaging
    assert await cleanup is True
    assert all(path.read_bytes() == b"" for path in artifact_root.rglob("*.zip"))


@pytest.mark.asyncio
async def test_post_register_cancellation_never_scrubs_an_available_artifact(
    tmp_path: Path, artifact_repository
) -> None:
    repository, engine, batch_id = artifact_repository
    artifact_root = (tmp_path / "artifacts").absolute()
    result, publication, _, _ = _inputs(tmp_path)

    class Progress:
        async def report(self, *_args, **_kwargs):
            return None

    class Cancellation:
        calls = 0

        def checkpoint(self):
            self.calls += 1
            if self.calls == 3:
                raise RuntimeError("post-register cancellation")

    step = ArtifactPackagingStep(
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)),
        repository,
        batch_locks=FileStorage((tmp_path / "data").absolute()),
        marker_registry=RetentionRepository(create_session_factory(engine)),
        now_factory=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="post-register cancellation"):
        await step.run(
            result,
            publication,
            artifact_root=artifact_root,
            batch_id=batch_id,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=24),
            progress=Progress(),
            cancellation=Cancellation(),
        )

    snapshot = (await repository.list_for_file("file-a"))[0]
    archive = next(artifact_root.rglob("*.zip"))
    assert snapshot.available is True
    assert archive.stat().st_size == snapshot.size_bytes
    assert sha256(archive.read_bytes()).hexdigest() == snapshot.sha256


@pytest.mark.asyncio
async def test_packaging_scrubs_published_archive_when_registration_fails(
    tmp_path: Path,
) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    artifact_root = (tmp_path / "artifacts").absolute()

    class Repository:
        released = False

        async def acquire_content_write(
            self, batch_id, file_task_id, *, now, lease_seconds
        ):
            return ContentWriteGuard(
                batch_id=batch_id,
                file_task_id=file_task_id,
                token="guard-token",
                expires_at=now + timedelta(seconds=lease_seconds),
            )

        async def register(self, _bundle, _records, *, write_guard):
            assert write_guard.token == "guard-token"
            raise RuntimeError("planted registration failure")

        async def release_content_write(self, guard):
            assert guard.token == "guard-token"
            self.released = True

    repository = Repository()

    class Progress:
        async def report(self, *_args, **_kwargs):
            return None

    class Cancellation:
        def checkpoint(self):
            return None

    step = ArtifactPackagingStep(
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)),
        repository,
        batch_locks=FileStorage((tmp_path / "data").absolute()),
        marker_registry=_MarkerRegistry(),
    )
    with pytest.raises(RuntimeError, match="planted registration failure"):
        await step.run(
            result,
            publication,
            artifact_root=artifact_root,
            batch_id=BATCH_ID,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=24),
            progress=Progress(),
            cancellation=Cancellation(),
        )

    archives = list(artifact_root.rglob("*.zip"))
    assert len(archives) == 1
    assert archives[0].read_bytes() == b""
    assert repository.released is True


def test_artifact_rollback_preserves_a_same_name_replacement(
    tmp_path: Path,
) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    bundler = ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000))
    bundle = bundler.publish(
        result,
        publication,
        artifact_root=(tmp_path / "artifacts").absolute(),
        batch_id=BATCH_ID,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=24),
    )
    moved_original = bundle.path.with_name("moved-original.zip")
    os.rename(bundle.path, moved_original)
    replacement = b"replacement ZIP bytes must survive"
    bundle.path.write_bytes(replacement)

    with pytest.raises(ArtifactFailure) as caught:
        bundler.scrub_published(bundle)

    assert caught.value.code == ArtifactErrorCode.PUBLISH_FAILED.value
    assert bundle.path.read_bytes() == replacement
    assert moved_original.stat().st_size == bundle.size_bytes


@pytest.mark.asyncio
async def test_exact_retry_requires_fsync_before_repository_registration(
    tmp_path: Path, monkeypatch
) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    artifact_root = (tmp_path / "artifacts").absolute()
    from ocr_mcp_server.services import artifacts as module

    original_fsync = module._PinnedArtifactRoot.fsync
    fsync_calls = 0

    def fail_first_fsync(self):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            raise OSError("planted durability failure")
            assert not list(artifact_root.rglob(".artifact-stage-*"))
        return original_fsync(self)

    monkeypatch.setattr(module._PinnedArtifactRoot, "fsync", fail_first_fsync)

    sentinel = object()

    class Repository:
        calls = 0
        released = 0

        async def acquire_content_write(
            self, batch_id, file_task_id, *, now, lease_seconds
        ):
            return ContentWriteGuard(
                batch_id=batch_id,
                file_task_id=file_task_id,
                token="guard-token",
                expires_at=now + timedelta(seconds=lease_seconds),
            )

        async def register(self, _bundle, _records, *, write_guard):
            assert write_guard.token == "guard-token"
            self.calls += 1
            return sentinel

        async def release_content_write(self, guard):
            assert guard.token == "guard-token"
            self.released += 1

    class Progress:
        async def report(self, _stage, _counters=None):
            return None

    class Cancellation:
        def checkpoint(self):
            return None

    repository = Repository()
    step = ArtifactPackagingStep(
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)),
        repository,
        batch_locks=FileStorage((tmp_path / "data").absolute()),
        marker_registry=_MarkerRegistry(),
    )
    arguments = dict(
        artifact_root=artifact_root,
        batch_id=BATCH_ID,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=24),
        progress=Progress(),
        cancellation=Cancellation(),
    )
    with pytest.raises(ArtifactFailure) as caught:
        await step.run(result, publication, **arguments)
    assert caught.value.code == ArtifactErrorCode.PUBLISH_FAILED.value
    assert fsync_calls == 1
    assert repository.calls == 0
    assert repository.released == 1
    assert len(list((artifact_root / BATCH_ID).glob("artifact-*.zip"))) == 1
    assert not list((artifact_root / BATCH_ID).glob(".artifact-stage-*"))

    assert await step.run(result, publication, **arguments) is sentinel
    assert fsync_calls == 2
    assert repository.calls == 1
    assert not list((artifact_root / BATCH_ID).glob(".artifact-stage-*"))


@pytest.mark.asyncio
async def test_repository_rejects_content_planted_in_model_metadata_on_first_insert(tmp_path: Path, artifact_repository) -> None:
    repository, _, batch_id = artifact_repository
    result, publication, _, record = _inputs(tmp_path)
    bundle = ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
        result, publication, artifact_root=(tmp_path / "artifacts").absolute(), batch_id=batch_id,
        created_at=NOW, expires_at=NOW + timedelta(hours=24),
    )
    unsafe = replace(record, model_versions={"pipeline": "C:/Clients/acme.pdf"})
    forged_digest = replacement_audit_metadata_sha256(batch_id, "file-a", (unsafe,))
    guard = await repository.acquire_content_write(
        batch_id, "file-a", now=NOW, lease_seconds=60
    )
    with pytest.raises(ArtifactFailure) as caught:
        await repository.register(
            replace(bundle, audit_metadata_sha256=forged_digest),
            (unsafe,),
            write_guard=guard,
        )
    assert caught.value.code == ArtifactErrorCode.INDEX_CONFLICT.value
    await repository.release_content_write(guard)
    assert "acme.pdf" not in str(caught.value)
