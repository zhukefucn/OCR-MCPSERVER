"""Tests for content-free container smoke helpers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _load_smoke_module() -> ModuleType:
    path = REPOSITORY_ROOT / "scripts" / "smoke_pp_structure.py"
    spec = importlib.util.spec_from_file_location("smoke_pp_structure", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cpu_smoke_checks_versions_features_and_real_prediction(
    tmp_path: Path,
) -> None:
    smoke = _load_smoke_module()
    fixture = tmp_path / "fixture.ppm"
    fixture.write_text("P3\n1 1\n255\n255 255 255\n", encoding="ascii")
    model_config = tmp_path / "pp-structure-v3.yaml"
    model_config.write_text("pipeline_name: PP-StructureV3\n", encoding="utf-8")
    calls: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, **kwargs: object) -> None:
            calls["constructor"] = kwargs

        def predict(self, input: str, **kwargs: object):
            calls["input"] = input
            calls["predict"] = kwargs
            return iter([{"res": {"layout_det_res": {"boxes": []}}}])

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
    assert calls["constructor"] == {
        "device": "cpu",
        "paddlex_config": str(model_config),
        **fixed_features,
    }
    assert calls["input"] == str(fixture)
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
    model_config = tmp_path / "pp-structure-v3.yaml"
    model_config.write_text("pipeline_name: PP-StructureV3\n", encoding="utf-8")
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
            paddle_module=paddle,
            paddleocr_module=paddleocr,
            package_version=lambda name: {
                "paddlepaddle": "3.3.0",
                "paddleocr": "3.5.0",
            }[name],
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
    fixture = REPOSITORY_ROOT / "scripts" / "fixtures" / "pp_structure_smoke.ppm"

    assert fixture.is_file()
    assert fixture.read_text(encoding="ascii").startswith("P3\n")
