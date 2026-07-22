from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
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
)
from ocr_mcp_server.infra.artifact_repository import ArtifactRepository
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
from ocr_mcp_server.settings import ArtifactSettings


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def _audit_record() -> ReplacementAuditRecord:
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
        original_node_snapshot='{"secret":"not metadata"}',
        replacement_node_snapshot='{"recognized":"not metadata"}',
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
    record = _audit_record()
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
        artifact_root=(tmp_path / "artifacts-a").absolute(), batch_id="batch-a",
        created_at=NOW, expires_at=NOW + timedelta(hours=24),
    )
    second = ArtifactBundler(limits).publish(
        result, publication,
        artifact_root=(tmp_path / "artifacts-b").absolute(), batch_id="batch-a",
        created_at=NOW, expires_at=NOW + timedelta(hours=24),
    )
    assert first.sha256 == second.sha256
    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.storage_key == second.storage_key
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


def test_zip_retry_reconciles_identical_and_rejects_tamper_and_conflict(tmp_path: Path) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    bundler = ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000))
    kwargs = dict(artifact_root=(tmp_path / "artifacts").absolute(), batch_id="batch-a", created_at=NOW, expires_at=NOW + timedelta(hours=24))
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
            result, publication, artifact_root=(tmp_path / "artifacts").absolute(), batch_id="batch-a",
            created_at=NOW, expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.INVALID_INPUT.value


def test_zip_rejects_unsafe_model_identifier_before_manifest_write(tmp_path: Path) -> None:
    result, publication, _, record = _inputs(tmp_path)
    planted = replace(record, model_versions={"pipeline": "document text\n/path/name.pdf"})
    publication = replace(publication, records=(planted,))
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result, publication, artifact_root=(tmp_path / "artifacts").absolute(), batch_id="batch-a",
            created_at=NOW, expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.INVALID_INPUT.value
    assert "document text" not in str(caught.value)


def test_publish_interruption_leaves_no_visible_target_or_stage(tmp_path: Path, monkeypatch) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    artifact_root = (tmp_path / "artifacts").absolute()

    def interrupted(*_args, **_kwargs):
        raise OSError("planted path and content")

    monkeypatch.setattr("ocr_mcp_server.services.artifacts.os.link", interrupted)
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result, publication, artifact_root=artifact_root, batch_id="batch-a",
            created_at=NOW, expires_at=NOW + timedelta(hours=24),
        )
    assert caught.value.code == ArtifactErrorCode.PUBLISH_FAILED.value
    assert not list(artifact_root.rglob("*.zip"))
    assert not list(artifact_root.rglob(".artifact-stage-*"))


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
            result, publication, artifact_root=artifact_root, batch_id="batch-a",
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
            result, publication, artifact_root=root, batch_id="batch-a", created_at=NOW, expires_at=NOW + timedelta(hours=24)
        )
    assert missing.value.code == ArtifactErrorCode.UNSAFE_IMAGE.value
    image.symlink_to(result.images_directory / "unrelated.png")
    with pytest.raises(ArtifactFailure) as linked:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result, publication, artifact_root=root, batch_id="batch-a", created_at=NOW, expires_at=NOW + timedelta(hours=24)
        )
    assert linked.value.code == ArtifactErrorCode.UNSAFE_IMAGE.value
    image.unlink()
    image.write_bytes(b"used-image")
    with pytest.raises(ArtifactFailure) as limited:
        ArtifactBundler(ArtifactLimits(10, 100_000, 20, 100_000)).publish(
            result, publication, artifact_root=root, batch_id="batch-a", created_at=NOW, expires_at=NOW + timedelta(hours=24)
        )
    assert limited.value.code == ArtifactErrorCode.LIMIT_EXCEEDED.value


def test_zip_rejects_referenced_image_changed_after_task7(tmp_path: Path) -> None:
    result, publication, _, _ = _inputs(tmp_path)
    (result.images_directory / "used.png").write_bytes(b"changed-after-task7")
    with pytest.raises(ArtifactFailure) as caught:
        ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
            result, publication, artifact_root=(tmp_path / "artifacts").absolute(), batch_id="batch-a",
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
    first = await repository.register(bundle, publication.records)
    repeated = await repository.register(bundle, publication.records)
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
        await repository.register(replace(bundle, sha256="0" * 64), publication.records)
    assert conflict.value.code == ArtifactErrorCode.INDEX_CONFLICT.value

    different = replace(
        record,
        audit_id="audit-" + "e" * 64,
        candidate_id="candidate-" + "e" * 64,
        processing_record_id="secondary-" + "e" * 64,
    )
    with pytest.raises(ArtifactFailure) as audit_conflict:
        await repository.register(bundle, (different,))
    assert audit_conflict.value.code == ArtifactErrorCode.INDEX_CONFLICT.value

    unsafe_model = replace(record, model_versions={"pipeline": "client name.pdf\n/document text"})
    with pytest.raises(ArtifactFailure) as unsafe_metadata:
        await repository.register(bundle, (unsafe_model,))
    assert unsafe_metadata.value.code == ArtifactErrorCode.INDEX_CONFLICT.value

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
async def test_packaging_step_reports_exact_packaging_then_publishing_counters(tmp_path: Path, artifact_repository) -> None:
    repository, _, batch_id = artifact_repository
    result, publication, _, _ = _inputs(tmp_path)
    bundler = ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000))
    events = []

    class Progress:
        async def report(self, stage, counters=None):
            events.append((stage.value, counters.completed_units, counters.total_units, counters.unit.value))

    class Cancellation:
        def checkpoint(self):
            return None

    step = ArtifactPackagingStep(bundler, repository)
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
    assert artifact == (await repository.list_for_file("file-a"))[0]


@pytest.mark.asyncio
async def test_repository_rejects_content_planted_in_model_metadata_on_first_insert(tmp_path: Path, artifact_repository) -> None:
    repository, _, batch_id = artifact_repository
    result, publication, _, record = _inputs(tmp_path)
    bundle = ArtifactBundler(ArtifactLimits(1_000_000, 100_000, 20, 100_000)).publish(
        result, publication, artifact_root=(tmp_path / "artifacts").absolute(), batch_id=batch_id,
        created_at=NOW, expires_at=NOW + timedelta(hours=24),
    )
    unsafe = replace(record, model_versions={"pipeline": "client name.pdf\n/document text"})
    forged_digest = replacement_audit_metadata_sha256(batch_id, "file-a", (unsafe,))
    with pytest.raises(ArtifactFailure) as caught:
        await repository.register(
            replace(bundle, audit_metadata_sha256=forged_digest), (unsafe,)
        )
    assert caught.value.code == ArtifactErrorCode.INDEX_CONFLICT.value
    assert "document text" not in str(caught.value)
