"""Run one bounded, content-free PP-StructureV3 container smoke check."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable


EXPECTED_PADDLE_VERSION = "3.3.0"
EXPECTED_PADDLEOCR_VERSION = "3.5.0"
DEFAULT_MODEL_CONFIG = Path("/models/pp-structure-v3.yaml")
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


def _installed_version(distribution: str) -> str:
    return version(distribution)


def _require_file(path: Path, code: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise SmokeFailure(code)


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
    paddle_module: Any | None = None,
    paddleocr_module: Any | None = None,
    package_version: Callable[[str], str] = _installed_version,
) -> dict[str, str | int]:
    """Validate packages/device/models and execute one real prediction."""

    if device not in {"cpu", "gpu:0"}:
        raise SmokeFailure("device_argument")
    _require_file(fixture, "fixture_unavailable")
    _require_file(model_config, "model_unavailable")

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
    try:
        pipeline = paddleocr_module.PPStructureV3(
            device=device,
            paddlex_config=str(model_config),
            **_FIXED_FEATURES,
        )
        results = list(
            pipeline.predict(
                str(fixture),
                **_FIXED_FEATURES,
                use_table_orientation_classify=True,
                use_ocr_results_with_table_cells=True,
            )
        )
    finally:
        close = getattr(pipeline, "close", None)
        if callable(close):
            close()
    if not results:
        raise SmokeFailure("prediction_empty")

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
    try:
        with open(os.devnull, "w", encoding="utf-8") as output_sink:
            with redirect_stdout(output_sink), redirect_stderr(output_sink):
                summary = run_smoke(
                    device=args.device,
                    fixture=args.fixture,
                    model_config=configured_model,
                )
    except Exception:
        print("pp_structure_smoke_failed", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
