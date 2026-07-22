from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


def _domain_objects(tmp_path: Path):
    from ocr_mcp_server.domain import (
        CandidateReference,
        CandidateSourceKind,
        ImageCandidate,
        MinerUImageFormat,
        SecondaryOCREngine,
        SecondaryProcessingRecord,
    )

    reference = CandidateReference(
        source_kind=CandidateSourceKind.MINERU_NODE,
        page_index=0,
        node_index=3,
        json_pointer="/0/3",
        original_node_type="table",
        bbox=(10.0, 20.0, 300.0, 400.0),
    )
    candidate = ImageCandidate(
        candidate_id="candidate-one",
        file_task_id="local-task",
        result_version=1,
        sha256="a" * 64,
        size_bytes=10,
        image_format=MinerUImageFormat.PNG,
        width=2,
        height=3,
        primary_path=tmp_path / "images" / "one.png",
        alias_paths=(tmp_path / "images" / "one.png",),
        references=(reference,),
        node_type_hints=("table",),
    )
    record = SecondaryProcessingRecord.pending_for(
        candidate, engine=SecondaryOCREngine.PP_STRUCTURE_V3
    )
    return reference, candidate, record


def test_references_distinguish_mineru_nodes_from_standalone_inputs(
    tmp_path: Path,
) -> None:
    from ocr_mcp_server.domain import CandidateReference, CandidateSourceKind

    mineru, _, _ = _domain_objects(tmp_path)
    standalone = CandidateReference.standalone_input()

    assert mineru.source_kind is CandidateSourceKind.MINERU_NODE
    assert (mineru.page_index, mineru.node_index, mineru.json_pointer) == (0, 3, "/0/3")
    assert standalone.source_kind is CandidateSourceKind.STANDALONE_INPUT
    assert standalone.page_index is standalone.node_index is standalone.json_pointer is None
    with pytest.raises(ValueError):
        CandidateReference(
            source_kind=CandidateSourceKind.STANDALONE_INPUT,
            page_index=0,
            node_index=0,
            json_pointer="/0/0",
        )
    with pytest.raises(FrozenInstanceError):
        mineru.page_index = 1  # type: ignore[misc]


def test_secondary_result_is_immutable_and_expresses_all_decision_states() -> None:
    from ocr_mcp_server.domain import (
        OrthogonalAngle,
        SecondaryContentFormat,
        SecondaryOCREngine,
        SecondaryResultKind,
        SecondaryResultState,
        SecondaryOcrResult,
    )

    valid = SecondaryOcrResult(
        kind=SecondaryResultKind.TABLE,
        angle=OrthogonalAngle.DEG_90,
        content="<table></table>",
        content_format=SecondaryContentFormat.HTML,
        confidence=0.9,
        engine=SecondaryOCREngine.PP_STRUCTURE_V3,
        model_versions={"layout": "v1", "ocr": "v2"},
        state=SecondaryResultState.VALID,
    )

    assert dict(valid.model_versions) == {"layout": "v1", "ocr": "v2"}
    with pytest.raises(TypeError):
        valid.model_versions["layout"] = "changed"  # type: ignore[index]
    with pytest.raises(ValueError):
        SecondaryOcrResult(
            kind=SecondaryResultKind.OTHER,
            angle=OrthogonalAngle.DEG_0,
            content=None,
            content_format=None,
            confidence=1.1,
            engine=SecondaryOCREngine.PP_STRUCTURE_V3,
            model_versions={},
            state=SecondaryResultState.VALID,
        )
    with pytest.raises(ValueError):
        SecondaryOcrResult(
            kind=SecondaryResultKind.OTHER,
            angle=OrthogonalAngle.DEG_0,
            content=None,
            content_format=None,
            confidence=10**400,
            engine=SecondaryOCREngine.PP_STRUCTURE_V3,
            model_versions={},
            state=SecondaryResultState.VALID,
        )
    for state in (
        SecondaryResultState.INVALID,
        SecondaryResultState.UNCERTAIN,
        SecondaryResultState.FAILED,
    ):
        result = SecondaryOcrResult(
            kind=SecondaryResultKind.UNCERTAIN,
            angle=OrthogonalAngle.DEG_0,
            content=None,
            content_format=None,
            confidence=0.0,
            engine=SecondaryOCREngine.PP_STRUCTURE_V3,
            model_versions={},
            state=state,
        )
        assert result.state is state


def test_provider_protocol_fixes_engine_on_instance_and_has_one_candidate_argument() -> None:
    import inspect

    from ocr_mcp_server.domain import SecondaryOcrProvider

    recognize = inspect.signature(SecondaryOcrProvider.recognize)

    assert tuple(recognize.parameters) == ("self", "candidate")
    assert "engine" in SecondaryOcrProvider.__annotations__


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_candidate",
        "missing_record",
        "extra_record",
        "duplicate_record",
        "record_order",
    ],
)
def test_collection_rejects_duplicate_or_non_bijective_records(
    tmp_path: Path, mutation: str
) -> None:
    from dataclasses import replace

    from ocr_mcp_server.domain import (
        CandidateCollection,
        CandidateCollectionFailure,
        SecondaryOCREngine,
    )

    _, candidate, record = _domain_objects(tmp_path)
    candidates = (candidate,)
    records = (record,)
    if mutation == "duplicate_candidate":
        candidates = (candidate, candidate)
    elif mutation == "missing_record":
        records = ()
    elif mutation == "extra_record":
        records = (record, replace(record, record_id="extra", candidate_id="unknown"))
    elif mutation == "duplicate_record":
        records = (record, record)
    else:
        other = replace(
            candidate,
            candidate_id="candidate-two",
            sha256="b" * 64,
            primary_path=tmp_path / "images" / "two.png",
            alias_paths=(tmp_path / "images" / "two.png",),
        )
        other_record = type(record).pending_for(
            other, engine=SecondaryOCREngine.PP_STRUCTURE_V3
        )
        candidates = (candidate, other)
        records = (other_record, record)

    with pytest.raises(CandidateCollectionFailure) as exc_info:
        CandidateCollection(
            file_task_id="local-task",
            result_version=1,
            candidates=candidates,
            processing_records=records,
        )

    assert exc_info.value.code == "candidate_collection_invariant"
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None


def test_pending_record_is_deterministic_and_has_no_result(tmp_path: Path) -> None:
    from ocr_mcp_server.domain import SecondaryOCREngine, SecondaryProcessingStatus

    _, candidate, first = _domain_objects(tmp_path)
    second = type(first).pending_for(
        candidate, engine=SecondaryOCREngine.PP_STRUCTURE_V3
    )

    assert first == second
    assert first.status is SecondaryProcessingStatus.PENDING
    assert first.result is None


def test_contracts_copy_mutable_sequences_and_reject_boolean_versions(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from ocr_mcp_server.domain import (
        CandidateCollection,
        CandidateCollectionFailure,
        CandidateReference,
        CandidateSourceKind,
    )

    bbox = [0, 1, 2, 3]
    reference = CandidateReference(
        source_kind=CandidateSourceKind.MINERU_NODE,
        page_index=0,
        node_index=0,
        json_pointer="/0/0",
        original_node_type="image",
        bbox=bbox,  # type: ignore[arg-type]
    )
    bbox[0] = 999
    assert reference.bbox == (0, 1, 2, 3)

    _, candidate, record = _domain_objects(tmp_path)
    with pytest.raises(ValueError):
        replace(candidate, result_version=True)
    with pytest.raises(ValueError):
        replace(record, result_version=True)
    with pytest.raises(CandidateCollectionFailure):
        CandidateCollection(
            file_task_id="local-task",
            result_version=True,
            candidates=(candidate,),
            processing_records=(record,),
        )


def test_contracts_reject_invalid_runtime_enums_and_boolean_metadata(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from ocr_mcp_server.domain import (
        OrthogonalAngle,
        SecondaryOCREngine,
        SecondaryOcrResult,
        SecondaryResultKind,
        SecondaryResultState,
    )

    _, candidate, record = _domain_objects(tmp_path)
    for changes in (
        {"image_format": "bogus"},
        {"size_bytes": True},
        {"width": True},
        {"height": True},
    ):
        with pytest.raises(ValueError):
            replace(candidate, **changes)
    for changes in (
        {"status": "not-pending"},
        {"engine": "runtime-fallback"},
    ):
        with pytest.raises(ValueError):
            replace(record, **changes)

    base = dict(
        kind=SecondaryResultKind.OTHER,
        angle=OrthogonalAngle.DEG_0,
        content=None,
        content_format=None,
        confidence=0.5,
        engine=SecondaryOCREngine.PP_STRUCTURE_V3,
        model_versions={},
        state=SecondaryResultState.VALID,
    )
    for field, value in (
        ("kind", "other"),
        ("angle", 45),
        ("engine", "runtime-fallback"),
        ("state", "valid"),
    ):
        invalid = dict(base)
        invalid[field] = value
        with pytest.raises(ValueError):
            SecondaryOcrResult(**invalid)  # type: ignore[arg-type]
