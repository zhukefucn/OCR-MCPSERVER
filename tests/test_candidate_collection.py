from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PIL import Image, features

from ocr_mcp_server.domain import (
    CandidateCollectionFailure,
    CandidateSourceKind,
    MinerUDocumentResult,
    MinerUImageFormat,
    SecondaryOCREngine,
    StoredFile,
    SupportedMediaType,
)


def _write_image(
    path: Path,
    image_format: str = "PNG",
    *,
    size: tuple[int, int] = (8, 6),
    color: tuple[int, int, int] = (12, 34, 56),
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color=color).save(path, format=image_format)


def _published_result(tmp_path: Path, manifest: object) -> MinerUDocumentResult:
    root = tmp_path / "published"
    parse = root / "parse"
    images = parse / "images"
    images.mkdir(parents=True)
    manifest_path = parse / "document_content_list_v2.json"
    if isinstance(manifest, str):
        manifest_path.write_text(manifest, encoding="utf-8")
    else:
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return MinerUDocumentResult(
        file_task_id="local-task",
        upstream_task_id="upstream-task",
        result_root=root,
        markdown_path=None,
        middle_json_path=None,
        content_list_v2_path=manifest_path,
        legacy_content_list_path=None,
        images_directory=images,
    )


def _collect(
    result: MinerUDocumentResult,
    *,
    result_version: int = 1,
    standalone_image: StoredFile | None = None,
    max_image_pixels: int = 10_000,
):
    from ocr_mcp_server.services.candidate_collection import collect_image_candidates

    return collect_image_candidates(
        result,
        result_version=result_version,
        engine=SecondaryOCREngine.PP_STRUCTURE_V3,
        standalone_image=standalone_image,
        max_image_pixels=max_image_pixels,
    )


def _stored_image(path: Path) -> StoredFile:
    import hashlib

    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    with Image.open(path) as image:
        width, height = image.size
    return StoredFile(
        file_id="stored-image",
        path=path,
        sha256=digest,
        size_bytes=path.stat().st_size,
        media_type=SupportedMediaType.PNG,
        extension=".png",
        page_count=1,
        width=width,
        height=height,
    )


def test_collects_all_structured_candidate_types_and_preserves_references(
    tmp_path: Path,
) -> None:
    nodes = [
        {
            "type": "image",
            "content": {"image_source": {"path": "images/image.png"}},
            "bbox": [10, 20, 300, 400],
        },
        {"type": "text", "content": {"text": "must not be inspected"}},
        {
            "type": "table",
            "content": {
                "image_source": {"path": "images/table.png"},
                "html": "<table></table>",
            },
        },
    ]
    second_page = [
        {
            "type": "equation_interline",
            "content": {
                "image_source": {"path": "images/formula.png"},
                "math_content": "x",
            },
        },
        {
            "type": "chart",
            "content": {"image_source": {"path": "images/chart.png"}},
        },
        {
            "type": "future_diagram",
            "content": {"image_source": {"path": "images/future.png"}},
        },
    ]
    result = _published_result(tmp_path, [nodes, second_page])
    for index, name in enumerate(
        ("image.png", "table.png", "formula.png", "chart.png", "future.png")
    ):
        _write_image(result.images_directory / name, color=(index, index, index))

    collection = _collect(result)

    assert [candidate.node_type_hints for candidate in collection.candidates] == [
        ("image",),
        ("table",),
        ("equation_interline",),
        ("chart",),
        ("future_diagram",),
    ]
    references = [candidate.references[0] for candidate in collection.candidates]
    assert [(ref.page_index, ref.node_index, ref.json_pointer) for ref in references] == [
        (0, 0, "/0/0"),
        (0, 2, "/0/2"),
        (1, 0, "/1/0"),
        (1, 1, "/1/1"),
        (1, 2, "/1/2"),
    ]
    assert references[0].bbox == (10, 20, 300, 400)
    assert len(collection.processing_records) == 5
    assert collection.total_reference_count == 5


def test_deduplicates_repeated_paths_and_identical_bytes_preserving_alias_order(
    tmp_path: Path,
) -> None:
    manifest = [[
        {"type": "image", "content": {"image_source": {"path": "images/a.png"}}},
        {"type": "table", "content": {"image_source": {"path": "images/a.png"}}},
        {"type": "chart", "content": {"image_source": {"path": "images/b.png"}}},
    ]]
    result = _published_result(tmp_path, manifest)
    _write_image(result.images_directory / "a.png")
    (result.images_directory / "b.png").write_bytes(
        (result.images_directory / "a.png").read_bytes()
    )

    collection = _collect(result)

    assert len(collection.candidates) == len(collection.processing_records) == 1
    candidate = collection.candidates[0]
    assert candidate.alias_paths == (
        result.images_directory / "a.png",
        result.images_directory / "b.png",
    )
    assert [reference.original_node_type for reference in candidate.references] == [
        "image",
        "table",
        "chart",
    ]
    assert candidate.node_type_hints == ("image", "table", "chart")


def test_distinct_bytes_keep_first_reference_order_and_ids_are_version_scoped(
    tmp_path: Path,
) -> None:
    manifest = [[
        {"type": "table", "content": {"image_source": {"path": "images/z.png"}}},
        {"type": "image", "content": {"image_source": {"path": "images/a.png"}}},
    ]]
    result = _published_result(tmp_path, manifest)
    _write_image(result.images_directory / "z.png", color=(1, 1, 1))
    _write_image(result.images_directory / "a.png", color=(2, 2, 2))

    first = _collect(result)
    rerun = _collect(result)
    next_version = _collect(result, result_version=2)

    assert [candidate.primary_path.name for candidate in first.candidates] == ["z.png", "a.png"]
    assert [candidate.candidate_id for candidate in first.candidates] == [
        candidate.candidate_id for candidate in rerun.candidates
    ]
    assert [record.record_id for record in first.processing_records] == [
        record.record_id for record in rerun.processing_records
    ]
    assert {candidate.candidate_id for candidate in first.candidates}.isdisjoint(
        candidate.candidate_id for candidate in next_version.candidates
    )
    assert {record.record_id for record in first.processing_records}.isdisjoint(
        record.record_id for record in next_version.processing_records
    )


def test_standalone_image_is_always_planned_and_deduplicates_with_mineru(
    tmp_path: Path,
) -> None:
    empty_result = _published_result(tmp_path / "empty", [[]])
    standalone_path = tmp_path / "original.part"
    _write_image(standalone_path)
    stored = _stored_image(standalone_path)

    only_original = _collect(empty_result, standalone_image=stored)

    assert len(only_original.candidates) == len(only_original.processing_records) == 1
    assert only_original.candidates[0].references == (
        only_original.candidates[0].references[0],
    )
    assert only_original.candidates[0].references[0].source_kind is CandidateSourceKind.STANDALONE_INPUT

    matching_result = _published_result(tmp_path / "matching", [[
        {"type": "image", "content": {"image_source": {"path": "images/copy.png"}}}
    ]])
    (matching_result.images_directory / "copy.png").write_bytes(standalone_path.read_bytes())

    combined = _collect(matching_result, standalone_image=stored)

    assert len(combined.candidates) == len(combined.processing_records) == 1
    assert [ref.source_kind for ref in combined.candidates[0].references] == [
        CandidateSourceKind.MINERU_NODE,
        CandidateSourceKind.STANDALONE_INPUT,
    ]
    assert combined.candidates[0].alias_paths == (
        matching_result.images_directory / "copy.png",
        standalone_path,
    )


@pytest.mark.parametrize(
    "manifest",
    [
        "{planted invalid json",
        {},
        [{"type": "image", "content": {"image_source": {"path": "images/a.png"}}}],
        [None],
        [[None]],
        [[{"type": "text", "content": []}]],
        [[{"type": "image", "content": {"image_source": []}}]],
        [[{"type": "image", "content": {"image_source": {}}}]],
        [[{"type": "image", "content": {"image_source": {"path": ""}}}]],
        [[{"type": "image", "content": {"image_source": {"path": 4}}}]],
        [[{"content": {"image_source": {"path": "images/a.png"}}}]],
        [[{"type": "image", "content": {"image_source": {"path": "images/a.png"}}, "bbox": [0, 1, 2]}]],
        [[{"type": "image", "content": {"image_source": {"path": "images/a.png"}}, "bbox": [0, 1, 1001, 2]}]],
        [[{"type": "image", "content": {"image_source": {"path": "images/a.png"}}, "bbox": [5, 1, 4, 2]}]],
        [[{"type": "image", "content": {"image_source": {"path": "images/a.png"}}, "bbox": [0, float("nan"), 4, 2]}]],
        [[{"type": "image", "content": {"image_source": {"path": "images/a.png"}}, "bbox": [0, 10**400, 4, 2]}]],
        [[{"type": 5, "content": {"image_source": {"path": "images/a.png"}}}]],
    ],
)
def test_rejects_malformed_manifest_schema_without_leaking_content(
    tmp_path: Path, manifest: object
) -> None:
    result = _published_result(tmp_path, manifest)

    with pytest.raises(CandidateCollectionFailure) as exc_info:
        _collect(result)

    assert exc_info.value.code == "candidate_manifest_invalid"
    assert "planted" not in repr(exc_info.value)
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/absolute.png",
        "../outside.png",
        "images/../outside.png",
        "images\\backslash.png",
        "C:/drive.png",
        "C:\\drive.png",
        "//server/share.png",
        "images//empty.png",
        "images/./dot.png",
        "images/control\u0001.png",
        "images/control\u0085.png",
    ],
)
def test_rejects_unsafe_candidate_paths(tmp_path: Path, unsafe_path: str) -> None:
    result = _published_result(tmp_path, [[
        {"type": "image", "content": {"image_source": {"path": unsafe_path}}}
    ]])
    if "\u0085" in unsafe_path:
        _write_image(result.content_list_v2_path.parent.joinpath(*unsafe_path.split("/")))

    with pytest.raises(CandidateCollectionFailure) as exc_info:
        _collect(result)

    assert exc_info.value.code == "candidate_path_unsafe_or_missing"
    assert unsafe_path not in repr(exc_info.value)
    assert exc_info.value.__context__ is None


def test_rejects_normalized_path_aliases(tmp_path: Path) -> None:
    result = _published_result(tmp_path, [[
        {"type": "image", "content": {"image_source": {"path": "images/SAME.png"}}},
        {"type": "image", "content": {"image_source": {"path": "images/same.png"}}},
    ]])
    _write_image(result.images_directory / "SAME.png")

    with pytest.raises(CandidateCollectionFailure) as exc_info:
        _collect(result)

    assert exc_info.value.code == "candidate_path_unsafe_or_missing"


@pytest.mark.parametrize("target_kind", ["missing", "directory"])
def test_rejects_missing_and_non_regular_candidates(tmp_path: Path, target_kind: str) -> None:
    result = _published_result(tmp_path, [[
        {"type": "image", "content": {"image_source": {"path": "images/target.png"}}}
    ]])
    if target_kind == "directory":
        (result.images_directory / "target.png").mkdir()

    with pytest.raises(CandidateCollectionFailure) as exc_info:
        _collect(result)

    assert exc_info.value.code == "candidate_path_unsafe_or_missing"


def test_rejects_symlink_and_outside_root_target(tmp_path: Path) -> None:
    result = _published_result(tmp_path, [[
        {"type": "image", "content": {"image_source": {"path": "images/link.png"}}}
    ]])
    outside = tmp_path / "private-outside.png"
    _write_image(outside)
    link = result.images_directory / "link.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(CandidateCollectionFailure) as exc_info:
        _collect(result)

    assert exc_info.value.code == "candidate_path_unsafe_or_missing"
    assert "private-outside" not in repr(exc_info.value)


def test_intermediate_component_race_cannot_open_outside_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ocr_mcp_server.services.candidate_collection as module

    result = _published_result(tmp_path, [[
        {"type": "image", "content": {"image_source": {"path": "images/switch/out.png"}}}
    ]])
    outside_directory = tmp_path / "outside-private"
    outside_path = outside_directory / "out.png"
    _write_image(outside_path)
    intermediate = result.images_directory / "switch"
    try:
        intermediate.symlink_to(outside_directory, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    real_lstat = module.os.lstat
    real_resolve = Path.resolve
    candidate_path = intermediate / "out.png"

    def raced_lstat(path):
        if Path(path) == intermediate:
            return os.stat(outside_directory)
        return real_lstat(path)

    def raced_resolve(path: Path, *, strict: bool = False):
        if path == candidate_path:
            return candidate_path.absolute()
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(module.os, "lstat", raced_lstat)
    monkeypatch.setattr(Path, "resolve", raced_resolve)

    with pytest.raises(CandidateCollectionFailure) as exc_info:
        _collect(result)

    assert exc_info.value.code == "candidate_path_unsafe_or_missing"
    assert "outside-private" not in repr(exc_info.value)


def test_posix_confined_open_anchors_every_root_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ocr_mcp_server.services.candidate_collection as module

    root = (tmp_path / "published" / "parse" / "images").absolute()
    candidate = root / "nested" / "image.png"
    directory_mode = os.stat(tmp_path).st_mode
    regular_file_stat = os.stat(__file__)
    open_calls: list[tuple[object, int | None]] = []
    next_descriptor = 10

    def fake_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal next_descriptor
        del flags, mode
        open_calls.append((path, dir_fd))
        next_descriptor += 1
        return next_descriptor

    def fake_fstat(descriptor: int):
        del descriptor
        return type("DirectoryStat", (), {"st_mode": directory_mode})()

    def fake_stat(path, *, dir_fd=None, follow_symlinks=True):
        del path, dir_fd, follow_symlinks
        return regular_file_stat

    monkeypatch.setattr(module.os, "open", fake_open)
    monkeypatch.setattr(module.os, "fstat", fake_fstat)
    monkeypatch.setattr(module.os, "stat", fake_stat)
    monkeypatch.setattr(module.os, "close", lambda descriptor: None)

    opened = module._open_posix_confined(candidate, root)

    assert Path(open_calls[0][0]) == Path(root.anchor)
    assert [str(path) for path, _ in open_calls[1:-1]] == [
        *root.parts[1:],
        "nested",
    ]
    assert str(open_calls[-1][0]) == "image.png"
    assert opened.parent_descriptor == open_calls[-1][1]


@pytest.mark.parametrize("fault_stage", ["fstat", "fdopen_value", "close"])
def test_descriptor_faults_are_normalized_without_retaining_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_stage: str
) -> None:
    import ocr_mcp_server.services.candidate_collection as module

    result = _published_result(tmp_path, [[
        {"type": "image", "content": {"image_source": {"path": "images/fault.png"}}}
    ]])
    _write_image(result.images_directory / "fault.png")
    secret = "planted-fstat-context"

    if fault_stage == "fstat":
        def fail_fstat(descriptor: int):
            raise OSError(secret)

        monkeypatch.setattr(module.os, "fstat", fail_fstat)
    elif fault_stage == "fdopen_value":
        def fail_fdopen(*args, **kwargs):
            raise ValueError(secret)

        monkeypatch.setattr(module.os, "fdopen", fail_fdopen)
    else:
        real_close = module.os.close
        injected = False

        def fail_close(descriptor: int):
            nonlocal injected
            real_close(descriptor)
            if not injected:
                injected = True
                raise OSError(secret)

        monkeypatch.setattr(module.os, "close", fail_close)

    with pytest.raises(CandidateCollectionFailure) as exc_info:
        _collect(result)

    assert exc_info.value.code in {
        "candidate_path_unsafe_or_missing",
        "candidate_changed_during_inspection",
    }
    assert secret not in repr(exc_info.value)
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None


_FORMAT_CASES = [
    ("PNG", ".png", MinerUImageFormat.PNG),
    ("JPEG", ".jpg", MinerUImageFormat.JPEG),
    ("WEBP", ".webp", MinerUImageFormat.WEBP),
    ("GIF", ".gif", MinerUImageFormat.GIF),
    ("BMP", ".bmp", MinerUImageFormat.BMP),
    ("TIFF", ".tiff", MinerUImageFormat.TIFF),
]
if features.check("jpg_2000"):
    _FORMAT_CASES.append(("JPEG2000", ".jp2", MinerUImageFormat.JPEG2000))


@pytest.mark.parametrize(("pillow_format", "extension", "expected"), _FORMAT_CASES)
def test_accepts_supported_mineru_raster_formats(
    tmp_path: Path,
    pillow_format: str,
    extension: str,
    expected: MinerUImageFormat,
) -> None:
    relative = f"images/candidate{extension}"
    result = _published_result(tmp_path, [[
        {"type": "image", "content": {"image_source": {"path": relative}}}
    ]])
    _write_image(result.images_directory / f"candidate{extension}", pillow_format)

    collection = _collect(result)

    assert collection.candidates[0].image_format is expected
    assert (collection.candidates[0].width, collection.candidates[0].height) == (8, 6)


def test_rejects_oversized_later_frame_in_multiframe_tiff(tmp_path: Path) -> None:
    result = _published_result(tmp_path, [[
        {"type": "image", "content": {"image_source": {"path": "images/multi.tiff"}}}
    ]])
    path = result.images_directory / "multi.tiff"
    first = Image.new("RGB", (1, 1), color=(1, 2, 3))
    second = Image.new("RGB", (11, 10), color=(4, 5, 6))
    first.save(path, format="TIFF", save_all=True, append_images=[second])

    with pytest.raises(CandidateCollectionFailure) as exc_info:
        _collect(result, max_image_pixels=100)

    assert exc_info.value.code == "candidate_image_invalid_or_unsupported"


@pytest.mark.parametrize("kind", ["corrupt", "format_mismatch", "too_many_pixels", "bomb"])
def test_rejects_invalid_unsupported_or_excessive_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    result = _published_result(tmp_path, [[
        {"type": "image", "content": {"image_source": {"path": "images/candidate.png"}}}
    ]])
    path = result.images_directory / "candidate.png"
    if kind == "corrupt":
        path.write_bytes(b"planted corrupt image content")
    elif kind == "format_mismatch":
        _write_image(path, "JPEG")
    else:
        _write_image(path, size=(11, 10))
        if kind == "bomb":
            monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)

    with pytest.raises(CandidateCollectionFailure) as exc_info:
        _collect(result, max_image_pixels=100 if kind != "bomb" else 1_000)

    assert exc_info.value.code == "candidate_image_invalid_or_unsupported"
    assert "planted" not in repr(exc_info.value)
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None


def test_detects_file_identity_change_during_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ocr_mcp_server.services.candidate_collection as module

    result = _published_result(tmp_path, [[
        {"type": "image", "content": {"image_source": {"path": "images/changing.png"}}}
    ]])
    _write_image(result.images_directory / "changing.png")
    real_compare = module._same_file_identity
    comparisons = 0

    def changed_on_final_check(first, second):
        nonlocal comparisons
        comparisons += 1
        if comparisons >= 3:
            return False
        return real_compare(first, second)

    monkeypatch.setattr(module, "_same_file_identity", changed_on_final_check)

    with pytest.raises(CandidateCollectionFailure) as exc_info:
        _collect(result)

    assert exc_info.value.code == "candidate_changed_during_inspection"
    assert exc_info.value.__context__ is None


def test_standalone_metadata_mismatch_is_treated_as_changed_content(
    tmp_path: Path,
) -> None:
    result = _published_result(tmp_path, [[]])
    path = tmp_path / "standalone.part"
    _write_image(path)
    stored = _stored_image(path)
    path.write_bytes(path.read_bytes() + b"changed")

    with pytest.raises(CandidateCollectionFailure) as exc_info:
        _collect(result, standalone_image=stored)

    assert exc_info.value.code == "candidate_changed_during_inspection"


def test_non_image_document_without_candidates_returns_empty_collection(
    tmp_path: Path,
) -> None:
    result = _published_result(tmp_path, [[
        {"type": "text", "content": {"text": "not logged or parsed"}}
    ]])

    collection = _collect(result)

    assert collection.candidates == ()
    assert collection.processing_records == ()
    assert collection.total_reference_count == 0


def test_collector_rejects_request_supplied_non_enum_engine(tmp_path: Path) -> None:
    from ocr_mcp_server.services.candidate_collection import collect_image_candidates

    result = _published_result(tmp_path, [[]])

    with pytest.raises(CandidateCollectionFailure) as exc_info:
        collect_image_candidates(
            result,
            result_version=1,
            engine="runtime-fallback",  # type: ignore[arg-type]
            max_image_pixels=100,
        )

    assert exc_info.value.code == "candidate_collection_invariant"


def test_gateway_does_not_add_forbidden_ocr_runtime_dependencies() -> None:
    project = Path(__file__).parents[1] / "pyproject.toml"
    normalized = project.read_text(encoding="utf-8").lower()

    assert "mineru" not in normalized
    assert "paddle" not in normalized
    assert "torch" not in normalized
