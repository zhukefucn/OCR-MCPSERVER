from __future__ import annotations

from pathlib import Path
import sys
import threading
from types import ModuleType

import pytest

from ocr_mcp_server.domain import (
    CandidateReference,
    CandidateSourceKind,
    ImageCandidate,
    MinerUImageFormat,
    OrthogonalAngle,
    SecondaryContentFormat,
    SecondaryOCREngine,
    SecondaryResultKind,
    SecondaryResultState,
)
from ocr_mcp_server.settings import SecondaryOCRSettings


def _candidate(tmp_path: Path, hint: str = "image") -> ImageCandidate:
    path = tmp_path / "bank-secret.png"
    path.write_bytes(b"fake")
    return ImageCandidate(
        candidate_id="candidate",
        file_task_id="task",
        result_version=1,
        sha256="a" * 64,
        size_bytes=4,
        image_format=MinerUImageFormat.PNG,
        width=1,
        height=1,
        primary_path=path,
        alias_paths=(path,),
        references=(
            CandidateReference(
                source_kind=CandidateSourceKind.MINERU_NODE,
                page_index=0,
                node_index=0,
                json_pointer="/0/0",
                original_node_type=hint,
            ),
        ) if hint else (CandidateReference.standalone_input(),),
        node_type_hints=(hint,) if hint else (),
    )


def _response(*, angle=0, boxes=None, tables=None, formulas=None):
    return [
        {
            "res": {
                "doc_preprocessor_res": {"angle": angle},
                "layout_det_res": {"boxes": boxes if boxes is not None else []},
                "table_res_list": tables if tables is not None else [],
                "formula_res_list": formulas if formulas is not None else [],
            }
        }
    ]


class _JsonResult:
    def __init__(self, value):
        self.json = value


class _MappingJsonResult(dict):
    """Real PaddleX results are dict subclasses whose JSON contract is a property."""

    def __init__(self, raw_value, json_value):
        super().__init__(raw_value)
        self.json = json_value


def test_module_import_does_not_import_paddle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "paddleocr", raising=False)
    sys.modules.pop("ocr_mcp_server.infra.pp_structure_v3", None)

    __import__("ocr_mcp_server.infra.pp_structure_v3")

    assert "paddleocr" not in sys.modules


@pytest.mark.parametrize("device", ["gpu", "gpu:0"])
def test_backend_lazy_imports_and_uses_fixed_gpu_constructor_and_predict_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, device: str
) -> None:
    calls: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, **kwargs):
            calls["constructor_thread"] = threading.get_ident()
            calls["constructor"] = kwargs

        def predict(self, input, **kwargs):
            calls["input"] = input
            calls["predict"] = kwargs
            return _response(
                boxes=[{"label": "table", "score": 0.92}],
                tables=[{"pred_html": "<table><tr><td>x</td></tr></table>"}],
            )

        def close(self):
            calls["close_thread"] = threading.get_ident()

    fake = ModuleType("paddleocr")
    fake.PPStructureV3 = FakePipeline  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paddleocr", fake)
    from ocr_mcp_server.infra.pp_structure_v3 import PPStructureV3Backend

    settings = SecondaryOCRSettings(
        device=device,
        paddlex_config=Path("/trusted/pipeline.yaml"),
        formula_model_name="Trusted-Formula",
    )
    backend = PPStructureV3Backend(settings)
    result = backend.recognize(_candidate(tmp_path))
    backend.close()

    assert calls["constructor"] == {
        "device": device,
        "paddlex_config": "/trusted/pipeline.yaml",
        "formula_recognition_model_name": "Trusted-Formula",
        "use_doc_orientation_classify": True,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "use_seal_recognition": False,
        "use_table_recognition": True,
        "use_formula_recognition": True,
        "use_chart_recognition": False,
        "use_region_detection": False,
    }
    assert calls["input"] == str(tmp_path / "bank-secret.png")
    assert calls["predict"] == {
        "use_doc_orientation_classify": True,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "use_seal_recognition": False,
        "use_table_recognition": True,
        "use_formula_recognition": True,
        "use_chart_recognition": False,
        "use_region_detection": False,
        "use_table_orientation_classify": True,
        "use_ocr_results_with_table_cells": True,
    }
    assert result.kind is SecondaryResultKind.TABLE
    assert result.model_versions == {
        "pipeline": "PP-StructureV3",
        "formula": "Trusted-Formula",
    }


def test_cpu_backend_disables_mkldnn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, **kwargs):
            calls["constructor"] = kwargs

    fake = ModuleType("paddleocr")
    fake.PPStructureV3 = FakePipeline  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paddleocr", fake)
    from ocr_mcp_server.infra.pp_structure_v3 import PPStructureV3Backend

    PPStructureV3Backend(SecondaryOCRSettings(device="cpu"))

    assert calls["constructor"]["enable_mkldnn"] is False  # type: ignore[index]


def test_mapping_result_prefers_json_property_over_raw_mapping() -> None:
    from ocr_mcp_server.infra.pp_structure_v3 import normalize_pp_structure_v3_result

    json_contract = _response(
        angle=90,
        boxes=[{"label": "formula", "score": 0.93}],
        formulas=[{"rec_formula": "x^2"}],
    )[0]
    raw_internal_mapping = {"res": {"unsafe_internal_shape": object()}}

    result = normalize_pp_structure_v3_result(
        [_MappingJsonResult(raw_internal_mapping, json_contract)],
        threshold=0.8,
        model_versions={"pipeline": "PP-StructureV3"},
    )

    assert result.kind is SecondaryResultKind.FORMULA
    assert result.state is SecondaryResultState.VALID
    assert result.angle is OrthogonalAngle.DEG_90
    assert result.content == "x^2"


def test_factory_refuses_vl_and_missing_paddle_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ocr_mcp_server.domain import SecondaryOcrFailure
    from ocr_mcp_server.infra.pp_structure_v3 import build_pp_structure_v3_backend

    with pytest.raises(SecondaryOcrFailure) as wrong_engine:
        build_pp_structure_v3_backend(
            SecondaryOCRSettings(engine=SecondaryOCREngine.PADDLEOCR_VL)
        )
    assert wrong_engine.value.code == "secondary_ocr_initialization_unavailable"

    monkeypatch.setitem(sys.modules, "paddleocr", None)
    with pytest.raises(SecondaryOcrFailure) as missing:
        build_pp_structure_v3_backend(SecondaryOCRSettings())
    assert missing.value.code == "secondary_ocr_initialization_unavailable"
    assert missing.value.__context__ is None


@pytest.mark.asyncio
async def test_provider_factory_runs_real_pipeline_lifecycle_on_owner_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, int]] = []

    class FakePipeline:
        def __init__(self, **kwargs):
            calls.append(("constructor", threading.get_ident()))

        def predict(self, input, **kwargs):
            calls.append(("predict", threading.get_ident()))
            return _response(boxes=[{"label": "figure", "score": 0.9}])

        def close(self):
            calls.append(("close", threading.get_ident()))

    fake = ModuleType("paddleocr")
    fake.PPStructureV3 = FakePipeline  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paddleocr", fake)
    from ocr_mcp_server.infra.pp_structure_v3 import create_pp_structure_v3_provider

    provider = create_pp_structure_v3_provider(SecondaryOCRSettings())
    await provider.start()
    result = await provider.recognize(_candidate(tmp_path))
    await provider.close()

    assert result.kind is SecondaryResultKind.OTHER
    assert [name for name, _ in calls] == ["constructor", "predict", "close"]
    assert len({thread_id for _, thread_id in calls}) == 1
    assert calls[0][1] != threading.get_ident()


@pytest.mark.parametrize(
    ("response", "kind", "state", "content_format", "confidence", "angle"),
    [
        (
            _response(
                angle=90,
                boxes=[{"label": "table", "score": 0.9}],
                tables=[{"pred_html": "<table></table>"}],
            ),
            SecondaryResultKind.TABLE,
            SecondaryResultState.VALID,
            SecondaryContentFormat.HTML,
            0.9,
            OrthogonalAngle.DEG_90,
        ),
        (
            [
                _JsonResult(
                    _response(
                        angle=270,
                        boxes=[{"label": "formula", "score": 0.95}],
                        formulas=[{"rec_formula": "x^2"}],
                    )[0]
                )
            ],
            SecondaryResultKind.FORMULA,
            SecondaryResultState.VALID,
            SecondaryContentFormat.LATEX,
            0.95,
            OrthogonalAngle.DEG_270,
        ),
        (
            _response(boxes=[{"label": "figure", "score": 0.88}]),
            SecondaryResultKind.OTHER,
            SecondaryResultState.VALID,
            None,
            0.88,
            OrthogonalAngle.DEG_0,
        ),
        (
            _response(boxes=[{"label": "figure", "score": 0.4}]),
            SecondaryResultKind.UNCERTAIN,
            SecondaryResultState.UNCERTAIN,
            None,
            0.4,
            OrthogonalAngle.DEG_0,
        ),
        (
            _response(boxes=[]),
            SecondaryResultKind.UNCERTAIN,
            SecondaryResultState.UNCERTAIN,
            None,
            0.0,
            OrthogonalAngle.DEG_0,
        ),
        (
            _response(
                boxes=[
                    {"label": "table", "score": 0.9},
                    {"label": "formula", "score": 0.91},
                ],
                tables=[{"pred_html": "<table></table>"}],
                formulas=[{"rec_formula": "x"}],
            ),
            SecondaryResultKind.UNCERTAIN,
            SecondaryResultState.UNCERTAIN,
            None,
            0.91,
            OrthogonalAngle.DEG_0,
        ),
        (
            _response(
                boxes=[{"label": "table", "score": 0.9}],
                tables=[{"pred_html": "<table></table>"}, {"pred_html": "<table></table>"}],
            ),
            SecondaryResultKind.UNCERTAIN,
            SecondaryResultState.UNCERTAIN,
            None,
            0.9,
            OrthogonalAngle.DEG_0,
        ),
        (
            _response(
                boxes=[{"label": "table", "score": 0.9}],
                tables=[{"pred_html": "<table></table>"}, {"pred_html": ""}],
            ),
            SecondaryResultKind.UNCERTAIN,
            SecondaryResultState.UNCERTAIN,
            None,
            0.9,
            OrthogonalAngle.DEG_0,
        ),
        (
            _response(boxes=[{"label": "formula", "score": 0.7}], formulas=[{"rec_formula": "x"}]),
            SecondaryResultKind.UNCERTAIN,
            SecondaryResultState.UNCERTAIN,
            None,
            0.7,
            OrthogonalAngle.DEG_0,
        ),
    ],
)
def test_normalization_decisions(
    response,
    kind,
    state,
    content_format,
    confidence,
    angle,
) -> None:
    from ocr_mcp_server.infra.pp_structure_v3 import normalize_pp_structure_v3_result

    result = normalize_pp_structure_v3_result(
        response,
        threshold=0.8,
        model_versions={"pipeline": "PP-StructureV3", "formula": "trusted"},
    )

    assert result.kind is kind
    assert result.state is state
    assert result.content_format is content_format
    assert result.confidence == confidence
    assert result.angle is angle


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        [{}, {}],
        [{"res": []}],
        _response(angle=45),
        _response(angle=90.0),
        _response(angle=True),
        _response(boxes={}),
        _response(boxes=[{"label": "", "score": 0.9}]),
        _response(boxes=[{"label": "table", "score": float("nan")}]),
        _response(boxes=[{"label": "table", "score": True}]),
        _response(tables={}),
        _response(formulas=[{"rec_formula": 123}]),
    ],
)
def test_malformed_response_returns_content_free_invalid_result(response) -> None:
    from ocr_mcp_server.infra.pp_structure_v3 import normalize_pp_structure_v3_result

    result = normalize_pp_structure_v3_result(
        response,
        threshold=0.8,
        model_versions={"pipeline": "PP-StructureV3"},
    )

    assert result.kind is SecondaryResultKind.UNCERTAIN
    assert result.state is SecondaryResultState.INVALID
    assert result.content is result.content_format is None
    assert result.confidence == 0


@pytest.mark.parametrize(
    ("res", "expected_kind"),
    [
        (
            {
                "doc_preprocessor_res": {"angle": 0},
                "layout_det_res": {
                    "boxes": [{"label": "table", "score": 0.9}]
                },
                "table_res_list": [{"pred_html": "<table></table>"}],
            },
            SecondaryResultKind.TABLE,
        ),
        (
            {
                "doc_preprocessor_res": {"angle": 0},
                "layout_det_res": {
                    "boxes": [{"label": "formula", "score": 0.9}]
                },
                "formula_res_list": [{"rec_formula": "x"}],
            },
            SecondaryResultKind.FORMULA,
        ),
        (
            {
                "doc_preprocessor_res": {"angle": 0},
                "layout_det_res": {
                    "boxes": [{"label": "figure", "score": 0.9}]
                },
            },
            SecondaryResultKind.OTHER,
        ),
    ],
)
def test_real_v2_shape_allows_omitted_empty_recognition_lists(
    res: dict[str, object], expected_kind: SecondaryResultKind
) -> None:
    from ocr_mcp_server.infra.pp_structure_v3 import normalize_pp_structure_v3_result

    result = normalize_pp_structure_v3_result(
        [{"res": res}],
        threshold=0.8,
        model_versions={"pipeline": "PP-StructureV3"},
    )

    assert result.kind is expected_kind
    assert result.state is SecondaryResultState.VALID


def test_prediction_exception_and_node_hint_do_not_leak_or_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    planted = "recognized-bank-text /private/customer.png <table>secret</table>"

    class FakePipeline:
        def __init__(self, **kwargs):
            pass

        def predict(self, input, **kwargs):
            raise RuntimeError(planted)

    fake = ModuleType("paddleocr")
    fake.PPStructureV3 = FakePipeline  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paddleocr", fake)
    from ocr_mcp_server.infra.pp_structure_v3 import PPStructureV3Backend

    result = PPStructureV3Backend(SecondaryOCRSettings()).recognize(
        _candidate(tmp_path, hint="table")
    )

    assert result.kind is SecondaryResultKind.UNCERTAIN
    assert result.state is SecondaryResultState.FAILED
    assert result.content is None
    assert planted not in repr(result)
    assert planted not in caplog.text


def test_mineru_table_hint_cannot_override_paddle_other_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakePipeline:
        def __init__(self, **kwargs):
            pass

        def predict(self, input, **kwargs):
            return _response(boxes=[{"label": "figure", "score": 0.91}])

    fake = ModuleType("paddleocr")
    fake.PPStructureV3 = FakePipeline  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paddleocr", fake)
    from ocr_mcp_server.infra.pp_structure_v3 import PPStructureV3Backend

    result = PPStructureV3Backend(SecondaryOCRSettings()).recognize(
        _candidate(tmp_path, hint="table")
    )

    assert result.kind is SecondaryResultKind.OTHER
    assert result.state is SecondaryResultState.VALID
    assert result.content is None


def test_no_forbidden_paddle_or_vl_dependency_or_import_is_added() -> None:
    import tomllib

    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = [
        value.lower()
        for group in (
            metadata["project"]["dependencies"],
            metadata["project"]["optional-dependencies"]["dev"],
        )
        for value in group
    ]
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("src").rglob("*.py")
    ).lower()

    assert not any(
        dependency.startswith(("paddle", "paddlex", "torch", "opencv"))
        for dependency in dependencies
    )
    assert "import paddleocr_vl" not in sources
    assert "from paddleocr_vl" not in sources
