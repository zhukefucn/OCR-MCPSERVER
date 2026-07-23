"""Run one bounded, content-free PP-StructureV3 container smoke check."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Callable, Iterator

import yaml


EXPECTED_PADDLE_VERSION = "3.3.0"
EXPECTED_PADDLEOCR_VERSION = "3.5.0"
MAX_METADATA_BYTES = 1024 * 1024
MAX_FIXTURE_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_FILES = 10_000
MAX_MODEL_DIRECTORIES = 512
MAX_IMAGE_WIDTH = 4096
MAX_IMAGE_HEIGHT = 4096
MAX_IMAGE_PIXELS = 16_000_000
MAX_TABLE_COLUMNS = 20
MAX_TABLE_ROWS = 100
MAX_TABLE_CELLS = 1000
DEFAULT_MODEL_CONFIG = Path("/models/pp-structure-v3.yaml")
DEFAULT_MODEL_MANIFEST = Path("/models/model-manifest.json")
DEFAULT_MODEL_ROOT = Path("/models")
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
TEXTLINE_DISABLED_PIPELINES = (
    "SubPipelines.GeneralOCR",
    "SubPipelines.TableRecognition.SubPipelines.GeneralOCR",
)
_FIXED_FEATURES = {
    "use_doc_orientation_classify": True,
    "use_doc_unwarping": False,
    "use_textline_orientation": False,
    "use_seal_recognition": False,
    "use_table_recognition": True,
    "use_formula_recognition": True,
    "use_chart_recognition": False,
    "use_region_detection": False,
}


class SmokeFailure(RuntimeError):
    """A content-free smoke failure identified only by a finite code."""


@contextmanager
def suppress_process_output() -> Iterator[None]:
    """Temporarily route process-level stdout/stderr file descriptors to null."""

    saved_stdout: int | None = None
    saved_stderr: int | None = None
    null_fd: int | None = None
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        saved_stdout = os.dup(1)
        saved_stderr = os.dup(2)
        null_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null_fd, 1)
        os.dup2(null_fd, 2)
        yield
    finally:
        if saved_stdout is not None:
            os.dup2(saved_stdout, 1)
        if saved_stderr is not None:
            os.dup2(saved_stderr, 2)
        for owned_fd in (null_fd, saved_stderr, saved_stdout):
            if owned_fd is not None:
                os.close(owned_fd)


def _installed_version(distribution: str) -> str:
    return version(distribution)


def _require_file(path: Path, code: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise SmokeFailure(code)


def _resolve_inside(
    root: Path,
    raw_path: object,
    *,
    kind: str,
) -> Path:
    relative = Path(raw_path) if isinstance(raw_path, str) else None
    if (
        relative is None
        or not raw_path
        or relative.is_absolute()
        or any(component in {".", ".."} for component in relative.parts)
    ):
        raise SmokeFailure("model_path")
    unresolved = root
    for component in relative.parts:
        unresolved /= component
        if unresolved.is_symlink():
            raise SmokeFailure("model_path")
    candidate = unresolved.resolve()
    if candidate == root or root not in candidate.parents:
        raise SmokeFailure("model_path")
    if kind == "file" and not candidate.is_file():
        raise SmokeFailure("model_unavailable")
    if kind == "directory" and not candidate.is_dir():
        raise SmokeFailure("model_unavailable")
    return candidate


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_mapping(path: Path, code: str) -> dict[str, Any]:
    try:
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_METADATA_BYTES:
            raise SmokeFailure(code)
        if path.suffix.lower() == ".json":
            loaded = json.loads(path.read_text(encoding="utf-8"))
        else:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except SmokeFailure:
        raise
    except Exception as exc:
        raise SmokeFailure(code) from exc
    if not isinstance(loaded, dict):
        raise SmokeFailure(code)
    return loaded


def _model_node(config: dict[str, Any], dotted_path: str) -> dict[str, Any]:
    current: object = config
    for component in dotted_path.split("."):
        if not isinstance(current, dict) or component not in current:
            raise SmokeFailure("model_config")
        current = current[component]
    if not isinstance(current, dict):
        raise SmokeFailure("model_config")
    return current


def _validate_all_local_model_dirs(value: object, root: Path) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "model_dir" and child is not None:
                _validate_absolute_model_dir(root, child)
            else:
                _validate_all_local_model_dirs(child, root)
    elif isinstance(value, list):
        for child in value:
            _validate_all_local_model_dirs(child, root)


def _validate_absolute_model_dir(root: Path, raw_path: object) -> Path:
    candidate = Path(raw_path) if isinstance(raw_path, str) else None
    if candidate is None or not candidate.is_absolute():
        raise SmokeFailure("model_path")
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise SmokeFailure("model_path") from exc
    unresolved = root
    for component in relative.parts:
        unresolved /= component
        if unresolved.is_symlink():
            raise SmokeFailure("model_path")
    resolved = unresolved.resolve()
    if root not in resolved.parents or not resolved.is_dir():
        raise SmokeFailure("model_path")
    return resolved


def prepare_offline_config(
    *,
    model_root: Path,
    model_config: Path,
    model_manifest: Path,
) -> Path:
    """Verify the offline manifest and create a fully local pipeline config."""

    try:
        if (
            model_root.is_symlink()
            or model_config.is_symlink()
            or model_manifest.is_symlink()
        ):
            raise SmokeFailure("model_path")
        root = model_root.resolve(strict=True)
    except OSError as exc:
        raise SmokeFailure("model_unavailable") from exc
    _require_file(model_config, "model_unavailable")
    _require_file(model_manifest, "model_unavailable")
    try:
        if model_config.resolve(strict=True).parent != root:
            raise SmokeFailure("model_path")
        if model_manifest.resolve(strict=True).parent != root:
            raise SmokeFailure("model_path")
    except OSError as exc:
        raise SmokeFailure("model_unavailable") from exc

    manifest = _load_mapping(model_manifest, "model_manifest")
    if manifest.get("schema_version") != 1:
        raise SmokeFailure("model_manifest")
    manifest_config = _resolve_inside(
        root, manifest.get("pipeline_config"), kind="file"
    )
    if manifest_config != model_config.resolve():
        raise SmokeFailure("model_manifest")

    raw_models = manifest.get("models")
    raw_files = manifest.get("files")
    if not isinstance(raw_models, dict) or set(raw_models) != set(REQUIRED_MODEL_NODES):
        raise SmokeFailure("model_manifest")
    if (
        not isinstance(raw_files, list)
        or not raw_files
        or len(raw_files) > MAX_MANIFEST_FILES
    ):
        raise SmokeFailure("model_manifest")

    verified_files: list[Path] = []
    seen_files: set[Path] = set()
    for entry in raw_files:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise SmokeFailure("model_manifest")
        expected_hash = entry.get("sha256")
        if (
            not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
        ):
            raise SmokeFailure("model_manifest")
        verified_file = _resolve_inside(root, entry.get("path"), kind="file")
        if verified_file in seen_files:
            raise SmokeFailure("model_manifest")
        seen_files.add(verified_file)
        if _file_sha256(verified_file) != expected_hash:
            raise SmokeFailure("model_hash")
        verified_files.append(verified_file)
    if manifest_config not in verified_files:
        raise SmokeFailure("model_manifest")

    config = _load_mapping(model_config, "model_config")
    if any(
        _model_node(config, pipeline_path).get("use_textline_orientation") is not False
        for pipeline_path in TEXTLINE_DISABLED_PIPELINES
    ):
        raise SmokeFailure("model_config")
    verified_model_dirs: set[Path] = set()
    for node_path in REQUIRED_MODEL_NODES:
        model_dir = _resolve_inside(root, raw_models[node_path], kind="directory")
        verified_model_dirs.add(model_dir)
        if not any(model_dir in file_path.parents for file_path in verified_files):
            raise SmokeFailure("model_manifest")
        configured_dir = _model_node(config, node_path).get("model_dir")
        if _validate_absolute_model_dir(root, configured_dir) != model_dir:
            raise SmokeFailure("model_path")
    _validate_all_local_model_dirs(config, root)
    listed_model_files = {
        file_path
        for file_path in verified_files
        if any(model_dir in file_path.parents for model_dir in verified_model_dirs)
    }
    if set(verified_files) != listed_model_files | {manifest_config}:
        raise SmokeFailure("model_manifest")

    files_on_disk: set[Path] = set()
    visited_directories: set[Path] = set()
    pending_directories = list(verified_model_dirs)
    scheduled_directories = set(verified_model_dirs)
    if len(pending_directories) > MAX_MODEL_DIRECTORIES:
        raise SmokeFailure("model_manifest")
    while pending_directories:
        current = pending_directories.pop()
        if current in visited_directories:
            continue
        visited_directories.add(current)
        try:
            entries = os.scandir(current)
        except OSError as exc:
            raise SmokeFailure("model_unavailable") from exc
        with entries:
            for entry in entries:
                candidate = Path(entry.path)
                if entry.is_symlink():
                    raise SmokeFailure("model_path")
                if entry.is_dir(follow_symlinks=False):
                    resolved_directory = candidate.resolve()
                    if root not in resolved_directory.parents:
                        raise SmokeFailure("model_path")
                    if resolved_directory not in scheduled_directories:
                        scheduled_directories.add(resolved_directory)
                        pending_directories.append(resolved_directory)
                        if len(scheduled_directories) > MAX_MODEL_DIRECTORIES:
                            raise SmokeFailure("model_manifest")
                elif entry.is_file(follow_symlinks=False):
                    resolved_candidate = candidate.resolve()
                    if root not in resolved_candidate.parents:
                        raise SmokeFailure("model_path")
                    files_on_disk.add(resolved_candidate)
                    if (
                        len(files_on_disk) > len(listed_model_files)
                        or len(files_on_disk) > MAX_MANIFEST_FILES
                        or resolved_candidate not in listed_model_files
                    ):
                        raise SmokeFailure("model_manifest")
                else:
                    raise SmokeFailure("model_path")
    if files_on_disk != listed_model_files:
        raise SmokeFailure("model_manifest")
    return model_config


def render_synthetic_fixture(fixture: Path) -> Path:
    """Render the repository-owned table blueprint to a realistic PNG."""

    blueprint = _load_mapping(fixture, "fixture_unavailable")
    width = blueprint.get("width")
    height = blueprint.get("height")
    columns = blueprint.get("columns")
    rows = blueprint.get("rows")
    if (
        blueprint.get("kind") != "synthetic_table"
        or type(width) is not int
        or type(height) is not int
        or width < 1200
        or width > MAX_IMAGE_WIDTH
        or height < 800
        or height > MAX_IMAGE_HEIGHT
        or width * height > MAX_IMAGE_PIXELS
        or not isinstance(columns, list)
        or len(columns) < 3
        or len(columns) > MAX_TABLE_COLUMNS
        or not isinstance(rows, list)
        or len(rows) < 4
        or len(rows) > MAX_TABLE_ROWS
        or (len(rows) + 1) * len(columns) > MAX_TABLE_CELLS
    ):
        raise SmokeFailure("fixture_contract")
    all_rows = [columns, *rows]
    if any(
        not isinstance(row, list)
        or len(row) != len(columns)
        or any(not isinstance(cell, str) or len(cell) > 32 for cell in row)
        for row in all_rows
    ):
        raise SmokeFailure("fixture_contract")

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    margin_x, margin_y = 80, 100
    table_width = width - 2 * margin_x
    table_height = height - 2 * margin_y
    cell_width = table_width // len(columns)
    cell_height = table_height // len(all_rows)
    draw.rectangle(
        (margin_x, margin_y, margin_x + table_width, margin_y + cell_height),
        fill="#e5e7eb",
    )
    for row_index in range(len(all_rows) + 1):
        y = margin_y + row_index * cell_height
        draw.line((margin_x, y, margin_x + table_width, y), fill="black", width=5)
    for column_index in range(len(columns) + 1):
        x = margin_x + column_index * cell_width
        draw.line((x, margin_y, x, margin_y + table_height), fill="black", width=5)
    for row_index, row in enumerate(all_rows):
        for column_index, cell in enumerate(row):
            draw.text(
                (
                    margin_x + column_index * cell_width + 20,
                    margin_y + row_index * cell_height + 20,
                ),
                cell,
                fill="black",
            )

    descriptor, temporary_name = tempfile.mkstemp(
        prefix="pp-structure-table-", suffix=".png"
    )
    os.close(descriptor)
    rendered = Path(temporary_name)
    try:
        image.save(rendered, format="PNG")
    except Exception:
        rendered.unlink(missing_ok=True)
        raise
    return rendered


def validate_image_fixture(fixture: Path) -> Path:
    """Validate a bounded PNG/JPEG fixture before Paddle sees it."""

    try:
        if fixture.is_symlink() or not fixture.is_file():
            raise SmokeFailure("fixture_unavailable")
        if not 0 < fixture.stat().st_size <= MAX_FIXTURE_BYTES:
            raise SmokeFailure("fixture_size")
        from PIL import Image

        with Image.open(fixture) as image:
            if image.format not in {"PNG", "JPEG"}:
                raise SmokeFailure("fixture_format")
            width, height = image.size
            if (
                width < 1
                or height < 1
                or width > MAX_IMAGE_WIDTH
                or height > MAX_IMAGE_HEIGHT
                or width * height > MAX_IMAGE_PIXELS
            ):
                raise SmokeFailure("fixture_dimensions")
            image.verify()
    except SmokeFailure:
        raise
    except Exception as exc:
        raise SmokeFailure("fixture_format") from exc
    return fixture


def _result_mapping(result: object) -> dict[str, Any] | None:
    missing = object()
    try:
        json_contract = inspect.getattr_static(result, "json", missing)
        value = result.json if json_contract is not missing else result
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def validate_prediction_contract(results: list[object]) -> None:
    """Require target evidence plus a corresponding structured result."""

    for raw_page in results:
        page = _result_mapping(raw_page)
        response = page.get("res") if page is not None else None
        if not isinstance(response, dict):
            continue
        layout = response.get("layout_det_res")
        boxes = layout.get("boxes") if isinstance(layout, dict) else None
        if not isinstance(boxes, list):
            continue
        labels = {
            box.get("label")
            for box in boxes
            if isinstance(box, dict) and box.get("label") in {"table", "formula"}
        }
        tables = response.get("table_res_list", [])
        formulas = response.get("formula_res_list", [])
        has_table = "table" in labels and isinstance(tables, list) and any(
            isinstance(item, dict)
            and isinstance(item.get("pred_html"), str)
            and bool(item["pred_html"].strip())
            for item in tables
        )
        has_formula = "formula" in labels and isinstance(formulas, list) and any(
            isinstance(item, dict)
            and isinstance(item.get("rec_formula"), str)
            and bool(item["rec_formula"].strip())
            for item in formulas
        )
        if has_table or has_formula:
            return
    raise SmokeFailure("prediction_contract")


def collect_single_result(results: object) -> list[object]:
    """Consume at most two iterator items and require exactly one page."""

    missing = object()
    try:
        iterator = iter(results)
        first = next(iterator)
        second = next(iterator, missing)
    except StopIteration as exc:
        raise SmokeFailure("prediction_count") from exc
    except Exception as exc:
        raise SmokeFailure("prediction_count") from exc
    if second is not missing:
        raise SmokeFailure("prediction_count")
    return [first]


def _is_distribution_installed(
    distribution: str,
    package_version: Callable[[str], str],
) -> bool:
    try:
        package_version(distribution)
    except (PackageNotFoundError, KeyError):
        return False
    return True


def run_smoke(
    *,
    device: str,
    fixture: Path,
    model_config: Path = DEFAULT_MODEL_CONFIG,
    model_manifest: Path = DEFAULT_MODEL_MANIFEST,
    model_root: Path = DEFAULT_MODEL_ROOT,
    paddle_module: Any | None = None,
    paddleocr_module: Any | None = None,
    package_version: Callable[[str], str] = _installed_version,
) -> dict[str, str | int]:
    """Validate packages/device/models and execute one real prediction."""

    if device not in {"cpu", "gpu:0"}:
        raise SmokeFailure("device_argument")
    _require_file(fixture, "fixture_unavailable")

    if paddle_module is None:
        import paddle as paddle_module
    if paddleocr_module is None:
        import paddleocr as paddleocr_module

    paddle_distribution = "paddlepaddle" if device == "cpu" else "paddlepaddle-gpu"
    if package_version(paddle_distribution) != EXPECTED_PADDLE_VERSION:
        raise SmokeFailure("paddle_version")
    if package_version("paddleocr") != EXPECTED_PADDLEOCR_VERSION:
        raise SmokeFailure("paddleocr_version")
    if str(paddle_module.__version__) != EXPECTED_PADDLE_VERSION:
        raise SmokeFailure("paddle_version")
    if str(paddleocr_module.__version__) != EXPECTED_PADDLEOCR_VERSION:
        raise SmokeFailure("paddleocr_version")

    compiled_with_cuda = bool(paddle_module.is_compiled_with_cuda())
    if device == "cpu":
        if compiled_with_cuda or _is_distribution_installed(
            "paddlepaddle-gpu", package_version
        ):
            raise SmokeFailure("device_mode")
    elif not compiled_with_cuda:
        raise SmokeFailure("device_mode")

    paddle_module.utils.run_check()
    pipeline = None
    verified_config = None
    rendered_fixture = None
    try:
        verified_config = prepare_offline_config(
            model_root=model_root,
            model_config=model_config,
            model_manifest=model_manifest,
        )
        prediction_fixture = fixture
        if fixture.suffix.lower() == ".json":
            rendered_fixture = render_synthetic_fixture(fixture)
            prediction_fixture = validate_image_fixture(rendered_fixture)
        else:
            prediction_fixture = validate_image_fixture(fixture)
        constructor_options = {
            "device": device,
            "paddlex_config": str(verified_config),
            **_FIXED_FEATURES,
        }
        if device == "cpu":
            constructor_options["enable_mkldnn"] = False
        pipeline = paddleocr_module.PPStructureV3(**constructor_options)
        results = collect_single_result(
            pipeline.predict(
                str(prediction_fixture),
                **_FIXED_FEATURES,
                use_table_orientation_classify=True,
                use_ocr_results_with_table_cells=True,
            )
        )
    finally:
        close = getattr(pipeline, "close", None)
        if callable(close):
            close()
        if rendered_fixture is not None:
            rendered_fixture.unlink(missing_ok=True)
    validate_prediction_contract(results)

    return {
        "paddle_version": EXPECTED_PADDLE_VERSION,
        "paddleocr_version": EXPECTED_PADDLEOCR_VERSION,
        "device": device,
        "result_count": len(results),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--device", required=True, choices=("cpu", "gpu:0"))
    parser.add_argument("--fixture", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configured_model = Path(
        os.environ.get(
            "OCR_SECONDARY_OCR__PADDLEX_CONFIG",
            DEFAULT_MODEL_CONFIG.as_posix(),
        )
    )
    configured_manifest = Path(
        os.environ.get(
            "OCR_PP_STRUCTURE_MODEL_MANIFEST",
            DEFAULT_MODEL_MANIFEST.as_posix(),
        )
    )
    try:
        with suppress_process_output():
            with open(os.devnull, "w", encoding="utf-8") as output_sink:
                with redirect_stdout(output_sink), redirect_stderr(output_sink):
                    summary = run_smoke(
                        device=args.device,
                        fixture=args.fixture,
                        model_config=configured_model,
                        model_manifest=configured_manifest,
                        model_root=DEFAULT_MODEL_ROOT,
                    )
    except Exception:
        print("pp_structure_smoke_failed", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
