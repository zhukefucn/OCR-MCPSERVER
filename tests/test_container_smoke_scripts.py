"""Tests for content-free container smoke helpers."""

from __future__ import annotations

import importlib.util
from hashlib import sha256
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _load_smoke_module() -> ModuleType:
    path = REPOSITORY_ROOT / "scripts" / "smoke_pp_structure.py"
    spec = importlib.util.spec_from_file_location("smoke_pp_structure", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REQUIRED_MODEL_NODES = (
    "SubModules.LayoutDetection",
    "SubPipelines.DocPreprocessor.SubModules.DocOrientationClassify",
    "SubPipelines.GeneralOCR.SubModules.TextDetection",
    "SubPipelines.GeneralOCR.SubModules.TextRecognition",
    "SubPipelines.TableRecognition.SubModules.TableClassification",
    "SubPipelines.TableRecognition.SubModules.WiredTableStructureRecognition",
    "SubPipelines.TableRecognition.SubModules.WirelessTableStructureRecognition",
    "SubPipelines.TableRecognition.SubModules.WiredTableCellsDetection",
    "SubPipelines.TableRecognition.SubModules.WirelessTableCellsDetection",
    "SubPipelines.TableRecognition.SubModules.TableOrientationClassify",
    "SubPipelines.FormulaRecognition.SubModules.FormulaRecognition",
)


def _offline_model_bundle(tmp_path: Path) -> tuple[Path, Path, Path]:
    model_root = tmp_path / "models"
    model_root.mkdir()
    config: dict[str, object] = {
        "pipeline_name": "PP-StructureV3",
        "SubModules": {"LayoutDetection": {"model_dir": None}},
        "SubPipelines": {
            "DocPreprocessor": {
                "SubModules": {"DocOrientationClassify": {"model_dir": None}}
            },
            "GeneralOCR": {
                "SubModules": {
                    "TextDetection": {"model_dir": None},
                    "TextRecognition": {"model_dir": None},
                }
            },
            "TableRecognition": {
                "SubModules": {
                    name: {"model_dir": None}
                    for name in (
                        "TableClassification",
                        "WiredTableStructureRecognition",
                        "WirelessTableStructureRecognition",
                        "WiredTableCellsDetection",
                        "WirelessTableCellsDetection",
                        "TableOrientationClassify",
                    )
                }
            },
            "FormulaRecognition": {
                "SubModules": {"FormulaRecognition": {"model_dir": None}}
            },
        },
    }
    model_entries: dict[str, str] = {}
    file_entries: list[dict[str, str]] = []
    for index, node_path in enumerate(REQUIRED_MODEL_NODES):
        relative_dir = Path("weights") / f"model-{index}"
        model_dir = model_root / relative_dir
        model_dir.mkdir(parents=True)
        model_file = model_dir / "inference.pdiparams"
        model_file.write_bytes(f"synthetic-model-{index}".encode())
        model_entries[node_path] = relative_dir.as_posix()
        file_entries.append(
            {
                "path": model_file.relative_to(model_root).as_posix(),
                "sha256": sha256(model_file.read_bytes()).hexdigest(),
            }
        )
    for node_path, relative_dir in model_entries.items():
        node = config
        for component in node_path.split("."):
            node = node[component]  # type: ignore[index]
        node["model_dir"] = str(model_root / relative_dir)  # type: ignore[index]
    config_path = model_root / "pp-structure-v3.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    file_entries.insert(
        0,
        {
            "path": config_path.relative_to(model_root).as_posix(),
            "sha256": sha256(config_path.read_bytes()).hexdigest(),
        },
    )
    manifest_path = model_root / "model-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pipeline_config": "pp-structure-v3.yaml",
                "models": model_entries,
                "files": file_entries,
            }
        ),
        encoding="utf-8",
    )
    return model_root, config_path, manifest_path


def test_cpu_smoke_checks_versions_features_and_real_prediction(
    tmp_path: Path,
) -> None:
    smoke = _load_smoke_module()
    fixture = REPOSITORY_ROOT / "scripts" / "fixtures" / "pp_structure_smoke.json"
    model_root, model_config, manifest_path = _offline_model_bundle(tmp_path)
    calls: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, **kwargs: object) -> None:
            calls["constructor"] = kwargs
            prepared_config = Path(str(kwargs["paddlex_config"]))
            calls["prepared_config"] = yaml.safe_load(
                prepared_config.read_text(encoding="utf-8")
            )

        def predict(self, input: str, **kwargs: object):
            calls["input"] = input
            calls["predict"] = kwargs
            return iter(
                [
                    {
                        "res": {
                            "layout_det_res": {
                                "boxes": [{"label": "table", "score": 0.99}]
                            },
                            "table_res_list": [
                                {"pred_html": "<table><tr><td>1</td></tr></table>"}
                            ],
                            "formula_res_list": [],
                        }
                    }
                ]
            )

        def close(self) -> None:
            calls["closed"] = True

    paddle = SimpleNamespace(
        __version__="3.3.0",
        is_compiled_with_cuda=lambda: False,
        utils=SimpleNamespace(run_check=lambda: calls.setdefault("run_check", True)),
    )
    paddleocr = SimpleNamespace(__version__="3.5.0", PPStructureV3=FakePipeline)

    summary = smoke.run_smoke(
        device="cpu",
        fixture=fixture,
        model_config=model_config,
        model_manifest=manifest_path,
        model_root=model_root,
        paddle_module=paddle,
        paddleocr_module=paddleocr,
        package_version=lambda name: {
            "paddlepaddle": "3.3.0",
            "paddleocr": "3.5.0",
        }[name],
    )

    fixed_features = {
        "use_doc_orientation_classify": True,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "use_seal_recognition": False,
        "use_table_recognition": True,
        "use_formula_recognition": True,
        "use_chart_recognition": False,
        "use_region_detection": False,
    }
    constructor = dict(calls["constructor"])
    prepared_config_path = Path(str(constructor.pop("paddlex_config")))
    assert constructor == {"device": "cpu", **fixed_features}
    assert prepared_config_path == model_config
    assert prepared_config_path.exists()
    prepared_config = calls["prepared_config"]
    for node_path in REQUIRED_MODEL_NODES:
        node = prepared_config
        for component in node_path.split("."):
            node = node[component]
        assert Path(node["model_dir"]).is_relative_to(model_root)
    assert Path(str(calls["input"])).suffix == ".png"
    assert not Path(str(calls["input"])).exists()
    assert calls["predict"] == {
        **fixed_features,
        "use_table_orientation_classify": True,
        "use_ocr_results_with_table_cells": True,
    }
    assert calls["run_check"] is True
    assert calls["closed"] is True
    assert summary == {
        "paddle_version": "3.3.0",
        "paddleocr_version": "3.5.0",
        "device": "cpu",
        "result_count": 1,
    }
    assert set(json.loads(json.dumps(summary))) == {
        "paddle_version",
        "paddleocr_version",
        "device",
        "result_count",
    }


def test_cpu_smoke_rejects_cuda_build_before_prediction(tmp_path: Path) -> None:
    smoke = _load_smoke_module()
    fixture = tmp_path / "fixture.ppm"
    fixture.write_text("P3\n1 1\n255\n255 255 255\n", encoding="ascii")
    model_root, model_config, manifest_path = _offline_model_bundle(tmp_path)
    paddleocr = SimpleNamespace(__version__="3.5.0", PPStructureV3=pytest.fail)
    paddle = SimpleNamespace(
        __version__="3.3.0",
        is_compiled_with_cuda=lambda: True,
        utils=SimpleNamespace(run_check=pytest.fail),
    )

    with pytest.raises(smoke.SmokeFailure, match="device_mode"):
        smoke.run_smoke(
            device="cpu",
            fixture=fixture,
            model_config=model_config,
            model_manifest=manifest_path,
            model_root=model_root,
            paddle_module=paddle,
            paddleocr_module=paddleocr,
            package_version=lambda name: {
                "paddlepaddle": "3.3.0",
                "paddleocr": "3.5.0",
            }[name],
        )


def test_smoke_rejects_non_target_nonempty_prediction(tmp_path: Path) -> None:
    smoke = _load_smoke_module()

    with pytest.raises(smoke.SmokeFailure, match="prediction_contract"):
        smoke.validate_prediction_contract(
            [{"res": {"layout_det_res": {"boxes": [{"label": "figure"}]}}}]
        )


def test_offline_model_manifest_rejects_missing_or_escaping_model_dir(
    tmp_path: Path,
) -> None:
    smoke = _load_smoke_module()
    model_root, config_path, manifest_path = _offline_model_bundle(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    missing_node = REQUIRED_MODEL_NODES[0]
    manifest["models"][missing_node] = "weights/missing"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(smoke.SmokeFailure, match="model_unavailable"):
        smoke.prepare_offline_config(
            model_root=model_root,
            model_config=config_path,
            model_manifest=manifest_path,
        )

    outside = tmp_path / "outside"
    outside.mkdir()
    manifest["models"][missing_node] = "../outside"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(smoke.SmokeFailure, match="model_path"):
        smoke.prepare_offline_config(
            model_root=model_root,
            model_config=config_path,
            model_manifest=manifest_path,
        )


def test_offline_model_manifest_rejects_hash_mismatch(tmp_path: Path) -> None:
    smoke = _load_smoke_module()
    model_root, config_path, manifest_path = _offline_model_bundle(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(smoke.SmokeFailure, match="model_hash"):
        smoke.prepare_offline_config(
            model_root=model_root,
            model_config=config_path,
            model_manifest=manifest_path,
        )


def test_offline_model_manifest_rejects_config_model_dir_mismatch(
    tmp_path: Path,
) -> None:
    smoke = _load_smoke_module()
    model_root, config_path, manifest_path = _offline_model_bundle(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["SubModules"]["LayoutDetection"]["model_dir"] = None
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["sha256"] = sha256(config_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(smoke.SmokeFailure, match="model_path"):
        smoke.prepare_offline_config(
            model_root=model_root,
            model_config=config_path,
            model_manifest=manifest_path,
        )


def test_offline_model_manifest_rejects_unlisted_model_file(tmp_path: Path) -> None:
    smoke = _load_smoke_module()
    model_root, config_path, manifest_path = _offline_model_bundle(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first_model_dir = model_root / manifest["models"][REQUIRED_MODEL_NODES[0]]
    (first_model_dir / "unlisted.params").write_bytes(b"not-in-manifest")

    with pytest.raises(smoke.SmokeFailure, match="model_manifest"):
        smoke.prepare_offline_config(
            model_root=model_root,
            model_config=config_path,
            model_manifest=manifest_path,
        )


def test_metadata_parser_rejects_oversized_file_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = _load_smoke_module()
    oversized = tmp_path / "manifest.json"
    oversized.write_bytes(b" " * (1024 * 1024 + 1))

    def unexpected_read(*args: object, **kwargs: object) -> str:
        pytest.fail("oversized metadata was read")

    monkeypatch.setattr(Path, "read_text", unexpected_read)
    with pytest.raises(smoke.SmokeFailure, match="model_manifest"):
        smoke._load_mapping(oversized, "model_manifest")


def test_prediction_contract_accepts_real_result_json_property() -> None:
    smoke = _load_smoke_module()

    class Result(dict):
        @property
        def json(self) -> dict[str, object]:
            return {
                "res": {
                    "layout_det_res": {
                        "boxes": [{"label": "formula", "score": 0.98}]
                    },
                    "formula_res_list": [{"rec_formula": "x^2"}],
                }
            }

    smoke.validate_prediction_contract([Result(internal="ignored")])


def test_offline_model_manifest_rejects_symlinked_metadata(tmp_path: Path) -> None:
    smoke = _load_smoke_module()
    model_root, config_path, manifest_path = _offline_model_bundle(tmp_path)
    real_manifest = model_root / "manifest-real.json"
    manifest_path.replace(real_manifest)
    try:
        manifest_path.symlink_to(real_manifest.name)
    except OSError:
        pytest.skip("file symlinks unavailable")

    with pytest.raises(smoke.SmokeFailure, match="model_path"):
        smoke.prepare_offline_config(
            model_root=model_root,
            model_config=config_path,
            model_manifest=manifest_path,
        )


def test_cli_suppresses_dependency_output_and_prints_only_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    smoke = _load_smoke_module()
    fixture = tmp_path / "fixture.ppm"
    fixture.write_text("P3\n1 1\n255\n255 255 255\n", encoding="ascii")

    def noisy_smoke(**kwargs: object) -> dict[str, str | int]:
        print("recognized customer content")
        print("/private/customer.png", file=__import__("sys").stderr)
        return {
            "paddle_version": "3.3.0",
            "paddleocr_version": "3.5.0",
            "device": "cpu",
            "result_count": 1,
        }

    monkeypatch.setattr(smoke, "run_smoke", noisy_smoke)

    assert smoke.main(["--device", "cpu", "--fixture", str(fixture)]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "paddle_version": "3.3.0",
        "paddleocr_version": "3.5.0",
        "device": "cpu",
        "result_count": 1,
    }
    assert captured.err == ""


def test_repository_owns_a_synthetic_ppstructure_fixture() -> None:
    smoke = _load_smoke_module()
    fixture = REPOSITORY_ROOT / "scripts" / "fixtures" / "pp_structure_smoke.json"

    assert fixture.is_file()
    rendered = smoke.render_synthetic_fixture(fixture)
    try:
        from PIL import Image

        with Image.open(rendered) as image:
            assert image.size[0] >= 1200
            assert image.size[1] >= 800
    finally:
        rendered.unlink(missing_ok=True)
