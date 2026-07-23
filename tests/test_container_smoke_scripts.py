"""Tests for content-free container smoke helpers."""

from __future__ import annotations

import importlib.util
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from zipfile import ZipFile

import httpx
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


def _load_mineru_smoke_module() -> ModuleType:
    path = REPOSITORY_ROOT / "scripts" / "smoke_mineru.py"
    spec = importlib.util.spec_from_file_location("smoke_mineru", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_deployment_verifier() -> ModuleType:
    path = REPOSITORY_ROOT / "scripts" / "verify_deployment.py"
    spec = importlib.util.spec_from_file_location("verify_deployment", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deployment_verifier_rejects_wrong_compose_and_unbounded_zip(
    tmp_path: Path,
) -> None:
    verifier = _load_deployment_verifier()
    valid = {
        "services": {
            "ocr-production": {
                "depends_on": {"mineru-api": {}},
                "ports": [{"published": "8000", "target": 8000}],
                "image": "ocr-mcp-server:production-dev",
            },
            "mineru-api": {
                "depends_on": {"mineru-vlm": {}},
                "image": "ocr-mcp-server:mineru-api-dev",
            },
            "mineru-vlm": {"image": "ocr-mcp-server:mineru-vlm-dev"},
        }
    }
    verifier.validate_compose(valid)
    with pytest.raises(verifier.VerificationFailure):
        verifier.validate_compose({"services": {}})

    payload = tmp_path / "result.zip"
    with ZipFile(payload, "w") as archive:
        archive.writestr("result.md", "bounded")
    assert verifier.validate_result_zip(payload) == 1
    with pytest.raises(verifier.VerificationFailure):
        verifier.validate_result_zip(payload, max_bytes=4)


def test_deployment_verifier_output_is_finite_and_secret_free() -> None:
    verifier = _load_deployment_verifier()
    event = verifier.safe_event("rest", True, count=3)
    assert event == '{"count":3,"ok":true,"stage":"rest"}'
    with pytest.raises(verifier.VerificationFailure):
        verifier.safe_event("rest", False, detail="document text")
    assert verifier.json_rows('[{"ID":"a"},{"ID":"b"}]') == [
        {"ID": "a"}, {"ID": "b"}
    ]
    assert verifier.json_rows('{"ID":"a"}\n{"ID":"b"}') == [
        {"ID": "a"}, {"ID": "b"}
    ]


def test_deployment_verifier_run_id_binds_candidate_and_is_stable() -> None:
    verifier = _load_deployment_verifier()
    git_sha = "a" * 40
    image_id = "sha256:" + "b" * 64
    run_id = verifier.make_run_id(git_sha, image_id)

    assert run_id == f"{git_sha}-sha256-{'b' * 64}"
    assert verifier.idempotency_keys(run_id) == (
        f"u-{run_id}",
        f"t-{run_id}",
    )
    assert verifier.make_run_id(git_sha, image_id) == run_id
    assert verifier.make_run_id("c" * 40, image_id) != run_id
    with pytest.raises(verifier.VerificationFailure):
        verifier.make_run_id("not-a-sha", image_id)


def test_deployment_verifier_terminates_unbounded_subprocess() -> None:
    verifier = _load_deployment_verifier()
    with pytest.raises(verifier.VerificationFailure, match="command_output"):
        verifier._run(
            [
                sys.executable,
                "-c",
                "import os\nwhile True: os.write(1, b'x' * 65536)",
            ],
            timeout=5,
            max_output=1024,
        )


def test_deployment_verifier_uses_bounded_manifest_and_zip_files(tmp_path: Path) -> None:
    verifier = _load_deployment_verifier()
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"models":[]}', encoding="utf-8")
    raw = verifier.read_regular_file_bounded(manifest, max_bytes=64)
    assert raw == b'{"models":[]}'

    too_large = tmp_path / "large.json"
    with too_large.open("wb") as stream:
        stream.truncate(65)
    with pytest.raises(verifier.VerificationFailure):
        verifier.read_regular_file_bounded(too_large, max_bytes=64)

    artifact = tmp_path / "result.zip"
    with ZipFile(artifact, "w") as archive:
        archive.writestr("result.md", "bounded")
    assert verifier.validate_result_zip(artifact, max_bytes=1024) == 1


def test_deployment_verifier_stream_parsers_are_bounded_and_match_mcp_id() -> None:
    verifier = _load_deployment_verifier()
    assert verifier.json_from_chunks([b'{"ok":', b"true}"], max_bytes=32) == {
        "ok": True
    }
    with pytest.raises(verifier.VerificationFailure):
        verifier.json_from_chunks([b"x" * 33], max_bytes=32)

    notification = b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
    answer = b'data: {"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n\n'
    assert verifier.mcp_json_from_chunks(
        [notification, answer], request_id=2, max_bytes=256
    )["id"] == 2

    def answer_then_unbounded() -> object:
        yield notification
        yield answer
        raise AssertionError("scanner consumed beyond the matching response")

    assert verifier.mcp_json_from_chunks(
        answer_then_unbounded(), request_id=2, max_bytes=256
    )["id"] == 2
    with pytest.raises(verifier.VerificationFailure):
        verifier.mcp_json_from_chunks([notification], request_id=2, max_bytes=256)


def test_deployment_verifier_binds_exact_services_images_and_health() -> None:
    verifier = _load_deployment_verifier()
    expected = {
        "ocr-production": "ocr-mcp-server:production-dev",
        "mineru-api": "ocr-mcp-server:mineru-api-dev",
        "mineru-vlm": "ocr-mcp-server:mineru-vlm-dev",
    }
    image_rows = [
        {"Service": service, "Repository": image.rsplit(":", 1)[0],
         "Tag": image.rsplit(":", 1)[1], "ID": f"sha256:{index:064x}"}
        for index, (service, image) in enumerate(expected.items(), start=1)
    ]
    verifier.validate_image_rows(image_rows, expected)
    with pytest.raises(verifier.VerificationFailure):
        verifier.validate_image_rows(image_rows[:-1], expected)

    ps_rows = [
        {"Service": "ocr-production", "Image": expected["ocr-production"],
         "State": "running", "Health": ""},
        {"Service": "mineru-api", "Image": expected["mineru-api"],
         "State": "running", "Health": "healthy"},
        {"Service": "mineru-vlm", "Image": expected["mineru-vlm"],
         "State": "running", "Health": "healthy"},
    ]
    verifier.validate_runtime_rows(ps_rows, expected)
    ps_rows[1]["Health"] = "starting"
    with pytest.raises(verifier.VerificationFailure):
        verifier.validate_runtime_rows(ps_rows, expected)


def test_deployment_verifier_validates_mcp_initialize_contract() -> None:
    verifier = _load_deployment_verifier()
    protocol = "2025-03-26"
    valid = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": protocol,
            "capabilities": {},
            "serverInfo": {"name": "ocr", "version": "1"},
        },
    }
    verifier.validate_initialize_response(valid, request_id=1, protocol=protocol)
    invalid = {**valid, "id": 99}
    with pytest.raises(verifier.VerificationFailure, match="mcp_initialize"):
        verifier.validate_initialize_response(invalid, request_id=1, protocol=protocol)


def test_deployment_verifier_log_gate_checks_all_services_and_canaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = _load_deployment_verifier()
    calls: list[list[str]] = []

    def clean_run(args: list[str], **_: object) -> str:
        calls.append(args)
        return "bounded operational metadata"

    monkeypatch.setattr(verifier, "_run", clean_run)
    verifier.verify_logs("secret-key")
    assert calls[0][-3:] == ["ocr-production", "mineru-api", "mineru-vlm"]

    monkeypatch.setattr(
        verifier,
        "_run",
        lambda *args, **kwargs: f"leak {verifier.SYNTHETIC_FILENAME_CANARY}",
    )
    with pytest.raises(verifier.VerificationFailure, match="log_boundary"):
        verifier.verify_logs("secret-key")


def test_mineru_smoke_synthetic_pdf_is_parseable() -> None:
    from pypdf import PdfReader

    smoke = _load_mineru_smoke_module()
    pdf = smoke.synthetic_pdf()
    document = PdfReader(BytesIO(pdf))

    assert len(document.pages) == 1
    stream = pdf.split(b"stream\n", 1)[1].split(b"\nendstream", 1)[0] + b"\n"
    declared_length = int(pdf.split(b"/Length ", 1)[1].split(b" ", 1)[0])
    assert declared_length == len(stream)


def test_mineru_runtime_smoke_imports_cv2_and_renders_cjk_in_memory() -> None:
    smoke = _load_mineru_smoke_module()
    calls: dict[str, object] = {}

    class FakeImage:
        def getbbox(self):
            return (1, 1, 8, 8)

        def save(self, output: BytesIO, format: str) -> None:
            calls["format"] = format
            output.write(b"synthetic-png")

    class ImageModule:
        @staticmethod
        def new(mode: str, size: tuple[int, int], color: int) -> FakeImage:
            calls["image"] = (mode, size, color)
            return FakeImage()

    class ImageDrawModule:
        @staticmethod
        def Draw(image: FakeImage):
            class Drawer:
                @staticmethod
                def text(
                    position: tuple[int, int],
                    text: str,
                    *,
                    fill: int,
                    font: object,
                ) -> None:
                    calls["draw"] = (position, text, fill, font)

            return Drawer()

    class ImageFontModule:
        @staticmethod
        def truetype(path: str, size: int) -> object:
            calls["font"] = (path, size)
            return "font"

    modules = {
        "cv2": SimpleNamespace(__version__="4.11.0"),
        "PIL.Image": ImageModule,
        "PIL.ImageDraw": ImageDrawModule,
        "PIL.ImageFont": ImageFontModule,
    }
    summary = smoke.check_runtime_dependencies(
        import_module=lambda name: modules[name],
        font_match=lambda: "/fonts/controller-selected.ttc",
    )

    assert calls["draw"] == ((4, 4), "\u4e2d", 255, "font")
    assert calls["format"] == "PNG"
    assert summary == {
        "opencv": "available",
        "pillow": "available",
        "cjk_font": "available",
    }


def test_mineru_runtime_smoke_uses_fontconfig_noto_cjk_match() -> None:
    smoke = _load_mineru_smoke_module()
    calls: dict[str, object] = {}

    def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls["command"] = command
        calls["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout="Noto Sans CJK SC\t/fonts/controller-selected.ttc\n",
        )

    assert smoke.resolve_cjk_font(command_runner=runner) == (
        "/fonts/controller-selected.ttc"
    )
    assert calls["command"][0] == "fc-match"
    assert "Noto Sans CJK" in calls["command"][-1]
    assert calls["kwargs"]["timeout"] <= 5


def test_mineru_runtime_only_cli_prints_content_free_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    smoke = _load_mineru_smoke_module()
    monkeypatch.setattr(
        smoke,
        "check_runtime_dependencies",
        lambda: {
            "opencv": "available",
            "pillow": "available",
            "cjk_font": "available",
        },
    )

    assert smoke.main(["--runtime-only"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "opencv": "available",
        "pillow": "available",
        "cjk_font": "available",
    }
    assert captured.err == ""


@pytest.mark.asyncio
async def test_mineru_smoke_checks_versions_cuda_services_and_zip_markdown() -> None:
    smoke = _load_mineru_smoke_module()
    seen: list[tuple[str, str]] = []
    zip_buffer = BytesIO()
    with ZipFile(zip_buffer, "w") as archive:
        archive.writestr("result.md", "synthetic result")

    async def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.host == "mineru-vlm" and request.url.path == "/health":
            return httpx.Response(200)
        if request.url.host == "mineru-vlm" and request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "fixed-model"}]})
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy"})
        if request.method == "POST":
            body = await request.aread()
            assert b"vlm-http-client" in body
            assert b"http://mineru-vlm:30000" in body
            return httpx.Response(
                202,
                json={
                    "task_id": "task-1",
                    "status_url": "http://mineru-api:8000/tasks/task-1",
                    "result_url": "http://mineru-api:8000/tasks/task-1/result",
                },
            )
        if request.url.path == "/tasks/task-1":
            return httpx.Response(200, json={"status": "completed"})
        return httpx.Response(
            200,
            content=zip_buffer.getvalue(),
            headers={"content-type": "application/zip"},
        )

    summary = await smoke.run_smoke(
        api_base_url="http://mineru-api:8000",
        vlm_base_url="http://mineru-vlm:30000",
        package_version=lambda name: {
            "mineru": "3.2.0",
            "vllm": "0.11.2+cu129",
        }[name],
        cuda_capability=lambda: (12, 0),
        transport=httpx.MockTransport(handler),
    )

    assert summary == {
        "mineru_version": "3.2.0",
        "vllm_version": "0.11.2",
        "cuda_capability": "12.0",
        "result_count": 1,
    }
    assert ("POST", "/tasks") in seen
    assert ("GET", "/tasks/task-1/result") in seen


@pytest.mark.asyncio
async def test_mineru_smoke_rejects_wrong_cuda_capability() -> None:
    smoke = _load_mineru_smoke_module()

    with pytest.raises(smoke.SmokeFailure, match="cuda_capability"):
        await smoke.run_smoke(
            api_base_url="http://mineru-api:8000",
            vlm_base_url="http://mineru-vlm:30000",
            package_version=lambda name: {
                "mineru": "3.2.0",
                "vllm": "0.11.2",
            }[name],
            cuda_capability=lambda: (8, 9),
            transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        )


@pytest.mark.parametrize(
    ("status_url", "result_url"),
    (
        (
            "http://attacker.invalid/tasks/task-1",
            "http://mineru-api:8000/tasks/task-1/result",
        ),
        (
            "http://mineru-api:8000/tasks/other",
            "http://mineru-api:8000/tasks/task-1/result",
        ),
        (
            "http://mineru-api:8000/tasks/task-1",
            "http://mineru-api:8000/tasks/task-1/result/extra",
        ),
    ),
)
def test_mineru_smoke_rejects_noncanonical_task_urls(
    status_url: str, result_url: str
) -> None:
    smoke = _load_mineru_smoke_module()

    with pytest.raises(smoke.SmokeFailure, match="task_submit"):
        smoke.validate_task_urls(
            api_base_url="http://mineru-api:8000",
            task_id="task-1",
            status_url=status_url,
            result_url=result_url,
        )


def test_mineru_smoke_rejects_zip_member_count_and_markdown_expansion() -> None:
    smoke = _load_mineru_smoke_module()
    too_many = BytesIO()
    with ZipFile(too_many, "w") as archive:
        for index in range(smoke.MAX_ZIP_MEMBERS + 1):
            archive.writestr(f"entry-{index}.txt", b"x")
        archive.writestr("result.md", b"valid")
    with pytest.raises(smoke.SmokeFailure, match="task_result"):
        smoke.validate_result_zip(too_many.getvalue())

    expanded = BytesIO()
    with ZipFile(expanded, "w") as archive:
        archive.writestr("result.md", b"x" * (smoke.MAX_MARKDOWN_BYTES + 1))
    with pytest.raises(smoke.SmokeFailure, match="task_result"):
        smoke.validate_result_zip(expanded.getvalue())


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


def _offline_model_bundle(
    tmp_path: Path,
    *,
    orientation_directory: str = "doc-orientation",
) -> tuple[Path, Path, Path]:
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
                "use_textline_orientation": False,
                "SubModules": {
                    "TextDetection": {"model_dir": None},
                    "TextRecognition": {"model_dir": None},
                }
            },
            "TableRecognition": {
                "SubPipelines": {
                    "GeneralOCR": {"use_textline_orientation": False}
                },
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
        relative_dir = (
            Path(orientation_directory)
            if node_path
            == "SubPipelines.DocPreprocessor.SubModules.DocOrientationClassify"
            else Path("weights") / f"model-{index}"
        )
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


def test_offline_manifest_requires_fixed_dedicated_orientation_directory(
    tmp_path: Path,
) -> None:
    smoke = _load_smoke_module()
    model_root, model_config, manifest_path = _offline_model_bundle(
        tmp_path, orientation_directory="weights/model-1"
    )

    with pytest.raises(smoke.SmokeFailure, match="model_manifest"):
        smoke.prepare_offline_config(
            model_root=model_root,
            model_config=model_config,
            model_manifest=manifest_path,
        )


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

    class FakeOrientation:
        def __init__(self, **kwargs):
            calls["orientation_constructor"] = kwargs

        def predict(self, input):
            calls["orientation_input"] = input
            return [{"label_names": ["0"], "scores": [0.99]}]

        def close(self):
            calls["orientation_closed"] = True

    paddle = SimpleNamespace(
        __version__="3.3.0",
        is_compiled_with_cuda=lambda: False,
        utils=SimpleNamespace(run_check=lambda: calls.setdefault("run_check", True)),
    )
    paddleocr = SimpleNamespace(
        __version__="3.5.0",
        PPStructureV3=FakePipeline,
        DocImgOrientationClassification=FakeOrientation,
    )

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
    assert constructor == {
        "device": "cpu",
        "enable_mkldnn": False,
        **fixed_features,
    }
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
    assert calls["orientation_constructor"] == {
        "device": "cpu",
        "enable_mkldnn": False,
        "model_dir": (model_root / "doc-orientation").as_posix(),
        "model_name": "PP-LCNet_x1_0_doc_ori",
        "topk": 1,
    }
    assert calls["orientation_closed"] is True
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


def test_gpu_smoke_checks_single_visible_gpu_and_real_prediction(
    tmp_path: Path,
) -> None:
    smoke = _load_smoke_module()
    fixture = REPOSITORY_ROOT / "scripts" / "fixtures" / "pp_structure_smoke.json"
    model_root, model_config, manifest_path = _offline_model_bundle(tmp_path)
    calls: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, **kwargs: object) -> None:
            calls["constructor"] = kwargs

        def predict(self, input: str, **kwargs: object):
            calls["predict"] = kwargs
            return iter(
                [
                    {
                        "res": {
                            "layout_det_res": {
                                "boxes": [{"label": "formula", "score": 0.99}]
                            },
                            "table_res_list": [],
                            "formula_res_list": [{"rec_formula": "x"}],
                        }
                    }
                ]
            )

        def close(self) -> None:
            calls["closed"] = True

    class FakeOrientation:
        def __init__(self, **kwargs):
            calls["orientation_constructor"] = kwargs

        def predict(self, input):
            return [{"label_names": ["90"], "scores": [0.98]}]

        def close(self):
            calls["orientation_closed"] = True

    paddle = SimpleNamespace(
        __version__="3.3.0",
        is_compiled_with_cuda=lambda: True,
        device=SimpleNamespace(
            cuda=SimpleNamespace(
                device_count=lambda: calls.setdefault("device_count", 1)
            )
        ),
        utils=SimpleNamespace(run_check=lambda: calls.setdefault("run_check", True)),
    )
    paddleocr = SimpleNamespace(
        __version__="3.5.0",
        PPStructureV3=FakePipeline,
        DocImgOrientationClassification=FakeOrientation,
    )

    summary = smoke.run_smoke(
        device="gpu:0",
        fixture=fixture,
        model_config=model_config,
        model_manifest=manifest_path,
        model_root=model_root,
        paddle_module=paddle,
        paddleocr_module=paddleocr,
        package_version=lambda name: {
            "paddlepaddle-gpu": "3.3.0",
            "paddleocr": "3.5.0",
        }[name],
    )

    constructor = calls["constructor"]
    assert constructor["device"] == "gpu:0"
    assert "enable_mkldnn" not in constructor
    assert calls["device_count"] == 1
    assert calls["run_check"] is True
    assert calls["closed"] is True
    assert calls["orientation_constructor"] == {
        "device": "gpu:0",
        "model_dir": (model_root / "doc-orientation").as_posix(),
        "model_name": "PP-LCNet_x1_0_doc_ori",
        "topk": 1,
    }
    assert calls["orientation_closed"] is True
    assert summary == {
        "paddle_version": "3.3.0",
        "paddleocr_version": "3.5.0",
        "device": "gpu:0",
        "result_count": 1,
    }


@pytest.mark.parametrize("visible_count", [0, 2])
def test_gpu_smoke_rejects_not_exactly_one_visible_gpu(
    tmp_path: Path, visible_count: int
) -> None:
    smoke = _load_smoke_module()
    fixture = REPOSITORY_ROOT / "scripts" / "fixtures" / "pp_structure_smoke.json"
    paddle = SimpleNamespace(
        __version__="3.3.0",
        is_compiled_with_cuda=lambda: True,
        device=SimpleNamespace(
            cuda=SimpleNamespace(device_count=lambda: visible_count)
        ),
        utils=SimpleNamespace(run_check=pytest.fail),
    )
    paddleocr = SimpleNamespace(__version__="3.5.0", PPStructureV3=pytest.fail)

    with pytest.raises(smoke.SmokeFailure, match="device_mode"):
        smoke.run_smoke(
            device="gpu:0",
            fixture=fixture,
            paddle_module=paddle,
            paddleocr_module=paddleocr,
            package_version=lambda name: {
                "paddlepaddle-gpu": "3.3.0",
                "paddleocr": "3.5.0",
            }[name],
        )


def test_gpu_smoke_rejects_installed_cpu_distribution(tmp_path: Path) -> None:
    smoke = _load_smoke_module()
    fixture = REPOSITORY_ROOT / "scripts" / "fixtures" / "pp_structure_smoke.json"
    paddle = SimpleNamespace(
        __version__="3.3.0",
        is_compiled_with_cuda=lambda: True,
        device=SimpleNamespace(cuda=SimpleNamespace(device_count=lambda: 1)),
        utils=SimpleNamespace(run_check=pytest.fail),
    )
    paddleocr = SimpleNamespace(__version__="3.5.0", PPStructureV3=pytest.fail)

    with pytest.raises(smoke.SmokeFailure, match="device_mode"):
        smoke.run_smoke(
            device="gpu:0",
            fixture=fixture,
            paddle_module=paddle,
            paddleocr_module=paddleocr,
            package_version=lambda name: {
                "paddlepaddle-gpu": "3.3.0",
                "paddlepaddle": "3.3.0",
                "paddleocr": "3.5.0",
            }[name],
        )


@pytest.mark.parametrize(
    "pipeline_path",
    (
        ("SubPipelines", "GeneralOCR"),
        ("SubPipelines", "TableRecognition", "SubPipelines", "GeneralOCR"),
    ),
)
def test_offline_config_requires_textline_orientation_disabled(
    tmp_path: Path, pipeline_path: tuple[str, ...]
) -> None:
    smoke = _load_smoke_module()
    model_root, config_path, manifest_path = _offline_model_bundle(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    pipeline = config
    for component in pipeline_path:
        pipeline = pipeline[component]
    pipeline["use_textline_orientation"] = True
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["sha256"] = sha256(config_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(smoke.SmokeFailure, match="model_config"):
        smoke.prepare_offline_config(
            model_root=model_root,
            model_config=config_path,
            model_manifest=manifest_path,
        )


def test_offline_manifest_excludes_disabled_textline_models() -> None:
    smoke = _load_smoke_module()

    assert all(
        "TextLineOrientation" not in node_path
        for node_path in smoke.REQUIRED_MODEL_NODES
    )


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


def test_offline_model_inventory_rejects_unbounded_directories(tmp_path: Path) -> None:
    smoke = _load_smoke_module()
    model_root, config_path, manifest_path = _offline_model_bundle(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first_model_dir = model_root / manifest["models"][REQUIRED_MODEL_NODES[0]]
    for index in range(smoke.MAX_MODEL_DIRECTORIES + 1):
        (first_model_dir / f"empty-{index}").mkdir()

    with pytest.raises(smoke.SmokeFailure, match="model_manifest"):
        smoke.prepare_offline_config(
            model_root=model_root,
            model_config=config_path,
            model_manifest=manifest_path,
        )


def test_manifest_file_count_matches_config_and_model_inventory(tmp_path: Path) -> None:
    smoke = _load_smoke_module()
    model_root, config_path, manifest_path = _offline_model_bundle(tmp_path)
    extra = model_root / "extra.txt"
    extra.write_bytes(b"extra")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append(
        {"path": "extra.txt", "sha256": sha256(extra.read_bytes()).hexdigest()}
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(smoke.SmokeFailure, match="model_manifest"):
        smoke.prepare_offline_config(
            model_root=model_root,
            model_config=config_path,
            model_manifest=manifest_path,
        )


def test_extra_config_model_dir_rejects_symlink_component(tmp_path: Path) -> None:
    smoke = _load_smoke_module()
    model_root, config_path, manifest_path = _offline_model_bundle(tmp_path)
    real_dir = model_root / "disabled-real"
    real_dir.mkdir()
    linked_dir = model_root / "disabled-link"
    try:
        linked_dir.symlink_to(real_dir, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["SubModules"]["Disabled"] = {"model_dir": str(linked_dir)}
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


def test_prediction_collection_consumes_at_most_two_and_requires_one() -> None:
    smoke = _load_smoke_module()
    consumed = 0

    def infinite_results():
        nonlocal consumed
        while True:
            consumed += 1
            yield {"res": {}}

    with pytest.raises(smoke.SmokeFailure, match="prediction_count"):
        smoke.collect_single_result(infinite_results())
    assert consumed == 2


def test_non_json_fixture_requires_bounded_verified_image(tmp_path: Path) -> None:
    smoke = _load_smoke_module()
    from PIL import Image

    unsupported = tmp_path / "fixture.bmp"
    Image.new("RGB", (100, 100), "white").save(unsupported)
    with pytest.raises(smoke.SmokeFailure, match="fixture_format"):
        smoke.validate_image_fixture(unsupported)

    too_wide = tmp_path / "fixture.png"
    Image.new("RGB", (smoke.MAX_IMAGE_WIDTH + 1, 10), "white").save(too_wide)
    with pytest.raises(smoke.SmokeFailure, match="fixture_dimensions"):
        smoke.validate_image_fixture(too_wide)

    oversized = tmp_path / "oversized.png"
    oversized.write_bytes(b"x" * (smoke.MAX_FIXTURE_BYTES + 1))
    with pytest.raises(smoke.SmokeFailure, match="fixture_size"):
        smoke.validate_image_fixture(oversized)


@pytest.mark.parametrize(
    "mutation",
    (
        {"width": 5000},
        {"height": 5000},
        {"columns": [str(index) for index in range(21)]},
        {"rows": [["1", "2", "3", "4", "5"] for _ in range(101)]},
        {
            "columns": [str(index) for index in range(20)],
            "rows": [["1"] * 20 for _ in range(60)],
        },
    ),
)
def test_synthetic_fixture_dimensions_and_cells_are_bounded(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    smoke = _load_smoke_module()
    source = REPOSITORY_ROOT / "scripts" / "fixtures" / "pp_structure_smoke.json"
    blueprint = json.loads(source.read_text(encoding="utf-8"))
    blueprint.update(mutation)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(blueprint), encoding="utf-8")

    with pytest.raises(smoke.SmokeFailure, match="fixture_contract"):
        smoke.render_synthetic_fixture(fixture)


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
    capfd: pytest.CaptureFixture[str],
) -> None:
    smoke = _load_smoke_module()
    fixture = tmp_path / "fixture.ppm"
    fixture.write_text("P3\n1 1\n255\n255 255 255\n", encoding="ascii")

    def noisy_smoke(**kwargs: object) -> dict[str, str | int]:
        print("recognized customer content")
        print("/private/customer.png", file=__import__("sys").stderr)
        __import__("os").write(1, b"fd-customer-content\n")
        __import__("os").write(2, b"fd-/private/customer.png\n")
        return {
            "paddle_version": "3.3.0",
            "paddleocr_version": "3.5.0",
            "device": "cpu",
            "result_count": 1,
        }

    monkeypatch.setattr(smoke, "run_smoke", noisy_smoke)

    assert smoke.main(["--device", "cpu", "--fixture", str(fixture)]) == 0

    captured = capfd.readouterr()
    assert json.loads(captured.out) == {
        "paddle_version": "3.3.0",
        "paddleocr_version": "3.5.0",
        "device": "cpu",
        "result_count": 1,
    }
    assert captured.err == ""


def test_cli_failure_suppresses_fd_output_and_prints_only_finite_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    smoke = _load_smoke_module()
    fixture = tmp_path / "fixture.png"
    fixture.write_bytes(b"placeholder")

    def noisy_failure(**kwargs: object) -> None:
        __import__("os").write(1, b"sensitive-stdout-canary\n")
        __import__("os").write(2, b"sensitive-stderr-canary\n")
        raise smoke.SmokeFailure("model_unavailable")

    monkeypatch.setattr(smoke, "run_smoke", noisy_failure)

    assert smoke.main(["--device", "cpu", "--fixture", str(fixture)]) == 1

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == "pp_structure_smoke_failed\n"


def test_fd_suppression_is_nested_exception_safe_and_closes_owned_fds(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    smoke = _load_smoke_module()
    opened: list[int] = []
    closed: list[int] = []
    real_dup = __import__("os").dup
    real_open = __import__("os").open
    real_close = __import__("os").close

    def tracking_dup(fd: int) -> int:
        owned = real_dup(fd)
        opened.append(owned)
        return owned

    def tracking_open(path: str, flags: int, mode: int = 0o777) -> int:
        owned = real_open(path, flags, mode)
        opened.append(owned)
        return owned

    def tracking_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    with monkeypatch.context() as patch:
        patch.setattr(smoke.os, "dup", tracking_dup)
        patch.setattr(smoke.os, "open", tracking_open)
        patch.setattr(smoke.os, "close", tracking_close)

        with pytest.raises(RuntimeError, match="expected"):
            with smoke.suppress_process_output():
                __import__("os").write(1, b"outer-sensitive\n")
                with smoke.suppress_process_output():
                    __import__("os").write(2, b"inner-sensitive\n")
                raise RuntimeError("expected")

    __import__("os").write(1, b"restored-stdout\n")
    __import__("os").write(2, b"restored-stderr\n")
    captured = capfd.readouterr()
    assert captured.out == "restored-stdout\n"
    assert captured.err == "restored-stderr\n"
    assert sorted(opened) == sorted(closed)


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
