"""Lazy PP-StructureV3 backend and conservative result normalization."""

from __future__ import annotations

import inspect
import math
from typing import Any, Mapping

from ..domain import (
    ImageCandidate,
    OrthogonalAngle,
    OrientationClassificationResult,
    SecondaryContentFormat,
    SecondaryOCREngine,
    SecondaryOcrErrorCode,
    SecondaryOcrFailure,
    SecondaryOcrResult,
    SecondaryResultKind,
    SecondaryResultState,
)
from ..settings import SecondaryOCRSettings
from .secondary_ocr import SingleOwnerSecondaryOcrWorker


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


class PPStructureV3Backend:
    """Synchronous backend; construct, call, and close only on its owner thread."""

    def __init__(self, settings: SecondaryOCRSettings) -> None:
        if settings.engine is not SecondaryOCREngine.PP_STRUCTURE_V3:
            raise SecondaryOcrFailure(
                SecondaryOcrErrorCode.INITIALIZATION_UNAVAILABLE
            )
        self._threshold = settings.classification_threshold
        self._model_versions = {
            "pipeline": "PP-StructureV3",
            "formula": settings.formula_model_name,
        }
        self._orientation_model_name = settings.orientation_model_name
        self._orientation_classifier = None
        initialization_failure: SecondaryOcrFailure | None = None
        try:
            from paddleocr import PPStructureV3

            constructor_options: dict[str, Any] = {
                "device": settings.device,
                "paddlex_config": (
                    settings.paddlex_config.as_posix()
                    if settings.paddlex_config is not None
                    else None
                ),
                "formula_recognition_model_name": settings.formula_model_name,
                **_FIXED_FEATURES,
            }
            if settings.device == "cpu":
                constructor_options["enable_mkldnn"] = False
            self._pipeline = PPStructureV3(**constructor_options)
            if settings.orientation_model_dir is not None:
                if (
                    not settings.orientation_model_dir.is_dir()
                    or settings.orientation_model_dir.is_symlink()
                ):
                    raise ValueError("orientation model directory unavailable")
                from paddleocr import DocImgOrientationClassification

                orientation_options: dict[str, Any] = {
                    "device": settings.device,
                    "model_dir": settings.orientation_model_dir.as_posix(),
                    "model_name": settings.orientation_model_name,
                    "topk": 1,
                }
                if settings.device == "cpu":
                    orientation_options["enable_mkldnn"] = False
                self._orientation_classifier = DocImgOrientationClassification(
                    **orientation_options
                )
        except BaseException as exc:
            initialization_failure = SecondaryOcrFailure(
                SecondaryOcrErrorCode.INITIALIZATION_UNAVAILABLE, cause=exc
            )
        if initialization_failure is not None:
            raise initialization_failure

    def recognize(self, candidate: ImageCandidate) -> SecondaryOcrResult:
        try:
            raw_result = self._pipeline.predict(
                str(candidate.primary_path),
                **_FIXED_FEATURES,
                use_table_orientation_classify=True,
                use_ocr_results_with_table_cells=True,
            )
        except BaseException:
            return _safe_result(
                state=SecondaryResultState.FAILED,
                model_versions=self._model_versions,
            )
        return normalize_pp_structure_v3_result(
            raw_result,
            threshold=self._threshold,
            model_versions=self._model_versions,
        )

    def classify_orientation(
        self, candidate: ImageCandidate
    ) -> OrientationClassificationResult:
        if self._orientation_classifier is None:
            return _safe_orientation_result(self._orientation_model_name)
        try:
            raw_result = self._orientation_classifier.predict(
                str(candidate.primary_path)
            )
        except BaseException:
            return _safe_orientation_result(self._orientation_model_name)
        return normalize_doc_orientation_result(
            raw_result, model_version=self._orientation_model_name
        )

    def close(self) -> None:
        if self._orientation_classifier is not None:
            orientation_close = getattr(self._orientation_classifier, "close", None)
            if callable(orientation_close):
                orientation_close()
        close = getattr(self._pipeline, "close", None)
        if callable(close):
            close()


def build_pp_structure_v3_backend(
    settings: SecondaryOCRSettings,
) -> PPStructureV3Backend:
    """Build the fixed production backend without exposing alternate engines."""

    if settings.engine is not SecondaryOCREngine.PP_STRUCTURE_V3:
        raise SecondaryOcrFailure(SecondaryOcrErrorCode.INITIALIZATION_UNAVAILABLE)
    return PPStructureV3Backend(settings)


def create_pp_structure_v3_provider(
    settings: SecondaryOCRSettings,
) -> SingleOwnerSecondaryOcrWorker:
    """Create the async provider whose owner thread lazily builds PP-Structure."""

    if settings.engine is not SecondaryOCREngine.PP_STRUCTURE_V3:
        raise SecondaryOcrFailure(SecondaryOcrErrorCode.INITIALIZATION_UNAVAILABLE)
    return SingleOwnerSecondaryOcrWorker(
        lambda: build_pp_structure_v3_backend(settings),
        queue_capacity=settings.queue_capacity,
    )


def normalize_pp_structure_v3_result(
    raw_result: object,
    *,
    threshold: float,
    model_versions: Mapping[str, str],
) -> SecondaryOcrResult:
    """Normalize only the documented JSON-safe PP-Structure response fields."""

    try:
        if not isinstance(raw_result, list) or len(raw_result) != 1:
            raise ValueError
        page = raw_result[0]
        missing = object()
        json_contract = inspect.getattr_static(page, "json", missing)
        if json_contract is not missing:
            page = page.json
        elif not isinstance(page, Mapping):
            raise ValueError
        if not isinstance(page, Mapping):
            raise ValueError
        response = page.get("res")
        if not isinstance(response, Mapping):
            raise ValueError

        preprocessor = response.get("doc_preprocessor_res")
        layout = response.get("layout_det_res")
        tables = response.get("table_res_list", [])
        formulas = response.get("formula_res_list", [])
        if (
            not isinstance(preprocessor, Mapping)
            or not isinstance(layout, Mapping)
            or not isinstance(tables, list)
            or not isinstance(formulas, list)
        ):
            raise ValueError

        raw_angle = preprocessor.get("angle")
        if type(raw_angle) is not int or raw_angle not in (0, 90, 180, 270):
            raise ValueError
        angle = OrthogonalAngle(raw_angle)
        boxes = layout.get("boxes")
        if not isinstance(boxes, list):
            raise ValueError
        evidence: list[tuple[str, float]] = []
        for box in boxes:
            if not isinstance(box, Mapping):
                raise ValueError
            label = box.get("label")
            score = box.get("score")
            if not isinstance(label, str) or not label or not _finite_score(score):
                raise ValueError
            evidence.append((label, float(score)))

        table_contents = _recognized_values(tables, "pred_html")
        formula_contents = _recognized_values(formulas, "rec_formula")
        confidence = max((score for _, score in evidence), default=0.0)
        target_boxes = [item for item in evidence if item[0] in {"table", "formula"}]
        target_result_count = len(table_contents) + len(formula_contents)
        raw_target_result_count = len(tables) + len(formulas)

        if (
            len(target_boxes) == 1
            and target_result_count == 1
            and raw_target_result_count == 1
        ):
            label, target_confidence = target_boxes[0]
            if target_confidence < threshold:
                return _safe_result(
                    state=SecondaryResultState.UNCERTAIN,
                    angle=angle,
                    confidence=target_confidence,
                    model_versions=model_versions,
                )
            if label == "table" and len(table_contents) == 1 and not formula_contents:
                return SecondaryOcrResult(
                    kind=SecondaryResultKind.TABLE,
                    angle=angle,
                    content=table_contents[0],
                    content_format=SecondaryContentFormat.HTML,
                    confidence=target_confidence,
                    engine=SecondaryOCREngine.PP_STRUCTURE_V3,
                    model_versions=model_versions,
                    state=SecondaryResultState.VALID,
                )
            if label == "formula" and len(formula_contents) == 1 and not table_contents:
                return SecondaryOcrResult(
                    kind=SecondaryResultKind.FORMULA,
                    angle=angle,
                    content=formula_contents[0],
                    content_format=SecondaryContentFormat.LATEX,
                    confidence=target_confidence,
                    engine=SecondaryOCREngine.PP_STRUCTURE_V3,
                    model_versions=model_versions,
                    state=SecondaryResultState.VALID,
                )

        if target_boxes or raw_target_result_count:
            return _safe_result(
                state=SecondaryResultState.UNCERTAIN,
                angle=angle,
                confidence=confidence,
                model_versions=model_versions,
            )
        if confidence >= threshold:
            return SecondaryOcrResult(
                kind=SecondaryResultKind.OTHER,
                angle=angle,
                content=None,
                content_format=None,
                confidence=confidence,
                engine=SecondaryOCREngine.PP_STRUCTURE_V3,
                model_versions=model_versions,
                state=SecondaryResultState.VALID,
            )
        return _safe_result(
            state=SecondaryResultState.UNCERTAIN,
            angle=angle,
            confidence=confidence,
            model_versions=model_versions,
        )
    except BaseException:
        return _safe_result(
            state=SecondaryResultState.INVALID,
            model_versions=model_versions,
        )


def _recognized_values(results: list[object], field: str) -> list[str]:
    values: list[str] = []
    for result in results:
        if not isinstance(result, Mapping):
            raise ValueError
        value = result.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError
        if value.strip():
            values.append(value)
    return values


def normalize_doc_orientation_result(
    raw_result: object,
    *,
    model_version: str,
) -> OrientationClassificationResult:
    """Normalize only the documented top-1 image-classification JSON fields."""

    try:
        if not isinstance(raw_result, list) or len(raw_result) != 1:
            raise ValueError
        result = raw_result[0]
        missing = object()
        json_contract = inspect.getattr_static(result, "json", missing)
        if json_contract is not missing:
            result = result.json
        elif not isinstance(result, Mapping):
            raise ValueError
        if not isinstance(result, Mapping):
            raise ValueError
        response = result.get("res")
        if not isinstance(response, Mapping):
            raise ValueError
        labels = response.get("label_names")
        scores = response.get("scores")
        if (
            not isinstance(labels, list)
            or not isinstance(scores, list)
            or len(labels) != 1
            or len(scores) != 1
            or labels[0] not in {"0", "90", "180", "270"}
            or not _finite_score(scores[0])
        ):
            raise ValueError
        return OrientationClassificationResult(
            angle=OrthogonalAngle(int(labels[0])),
            confidence=float(scores[0]),
            model_version=model_version,
        )
    except BaseException:
        return _safe_orientation_result(model_version)


def _finite_score(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def _safe_result(
    *,
    state: SecondaryResultState,
    model_versions: Mapping[str, str],
    angle: OrthogonalAngle = OrthogonalAngle.DEG_0,
    confidence: float = 0.0,
) -> SecondaryOcrResult:
    return SecondaryOcrResult(
        kind=SecondaryResultKind.UNCERTAIN,
        angle=angle,
        content=None,
        content_format=None,
        confidence=confidence,
        engine=SecondaryOCREngine.PP_STRUCTURE_V3,
        model_versions=model_versions,
        state=state,
    )


def _safe_orientation_result(model_version: str) -> OrientationClassificationResult:
    return OrientationClassificationResult(
        angle=OrthogonalAngle.DEG_0,
        confidence=0.0,
        model_version=model_version,
    )
