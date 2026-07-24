"""Lazy PP-StructureV3 backend and conservative result normalization."""

from __future__ import annotations

import inspect
import math
from dataclasses import dataclass
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
    SecondaryTextOrigin,
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
_TEXT_LAYOUT_LABELS = frozenset({
    "paragraph_title",
    "text",
    "number",
    "abstract",
    "content",
    "figure_title",
    "reference",
    "doc_title",
    "footnote",
    "header",
    "algorithm",
    "footer",
    "formula_number",
    "aside_text",
    "reference_content",
})
_STRUCTURED_LAYOUT_LABELS = frozenset({"table", "formula"})
_VISUAL_LAYOUT_LABELS = frozenset({"image", "seal", "chart"})


@dataclass(frozen=True, slots=True)
class _TextRecognitionPolicy:
    recognition_threshold: float
    min_characters: int
    mixed_min_lines: int
    mixed_min_characters: int
    max_lines: int
    max_characters: int
    max_utf8_bytes: int

    @classmethod
    def from_settings(
        cls,
        settings: SecondaryOCRSettings,
    ) -> _TextRecognitionPolicy:
        return cls(
            settings.text_recognition_threshold,
            settings.text_min_characters,
            settings.mixed_text_min_lines,
            settings.mixed_text_min_characters,
            settings.text_max_lines,
            settings.text_max_characters,
            settings.text_max_utf8_bytes,
        )

    @classmethod
    def defaults(cls) -> _TextRecognitionPolicy:
        return cls(
            recognition_threshold=0.8,
            min_characters=4,
            mixed_min_lines=2,
            mixed_min_characters=8,
            max_lines=2_000,
            max_characters=200_000,
            max_utf8_bytes=800_000,
        )


class PPStructureV3Backend:
    """Synchronous backend; construct, call, and close only on its owner thread."""

    def __init__(self, settings: SecondaryOCRSettings) -> None:
        if settings.engine is not SecondaryOCREngine.PP_STRUCTURE_V3:
            raise SecondaryOcrFailure(
                SecondaryOcrErrorCode.INITIALIZATION_UNAVAILABLE
            )
        self._threshold = settings.classification_threshold
        self._text_policy = _TextRecognitionPolicy.from_settings(settings)
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
            text_policy=self._text_policy,
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
    text_policy: _TextRecognitionPolicy | None = None,
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

        policy = text_policy or _TextRecognitionPolicy.defaults()
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
        target_boxes = [
            item for item in evidence if item[0] in _STRUCTURED_LAYOUT_LABELS
        ]
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

        plain_text, line_count, char_count, text_confidence = (
            _recognized_plain_text(response.get("overall_ocr_res"), policy)
        )
        high_confidence = [
            (label, score)
            for label, score in evidence
            if score >= threshold
        ]
        text_labels = {
            label
            for label, _ in high_confidence
            if label in _TEXT_LAYOUT_LABELS
        }
        visual_labels = {
            label
            for label, _ in high_confidence
            if label in _VISUAL_LAYOUT_LABELS
        }
        structured_evidence = [
            (label, score)
            for label, score in evidence
            if label in _STRUCTURED_LAYOUT_LABELS
        ]
        structured_confidence = max(
            (score for _, score in structured_evidence),
            default=0.0,
        )
        other_labels = {
            label
            for label, _ in high_confidence
            if label
            not in (
                _TEXT_LAYOUT_LABELS
                | _VISUAL_LAYOUT_LABELS
                | _STRUCTURED_LAYOUT_LABELS
            )
        }

        if structured_evidence or raw_target_result_count:
            if (
                structured_evidence
                and plain_text is not None
                and char_count >= policy.min_characters
            ):
                return _plain_text_result(
                    kind=SecondaryResultKind.IMAGE_WITH_TEXT,
                    origin=SecondaryTextOrigin.UNSTRUCTURED_FALLBACK,
                    angle=angle,
                    content=plain_text,
                    confidence=min(
                        structured_confidence,
                        text_confidence,
                    ),
                    model_versions=model_versions,
                )
            return _safe_result(
                state=SecondaryResultState.UNCERTAIN,
                angle=angle,
                confidence=max(confidence, structured_confidence),
                model_versions=model_versions,
            )

        if (
            text_labels
            and not visual_labels
            and plain_text is not None
            and char_count >= policy.min_characters
        ):
            kind = SecondaryResultKind.TEXT
            origin = SecondaryTextOrigin.TEXT_DOMINANT
        elif visual_labels and plain_text is not None and (
            line_count >= policy.mixed_min_lines
            or char_count >= policy.mixed_min_characters
        ):
            kind = SecondaryResultKind.IMAGE_WITH_TEXT
            origin = SecondaryTextOrigin.MIXED_VISUAL
        elif visual_labels:
            kind = SecondaryResultKind.OTHER
            origin = None
        elif plain_text is None and other_labels:
            kind = SecondaryResultKind.OTHER
            origin = None
        else:
            return _safe_result(
                state=SecondaryResultState.UNCERTAIN,
                angle=angle,
                confidence=confidence,
                model_versions=model_versions,
            )

        if kind is SecondaryResultKind.OTHER:
            return SecondaryOcrResult(
                kind=SecondaryResultKind.OTHER,
                angle=angle,
                content=None,
                content_format=None,
                confidence=confidence,
                engine=SecondaryOCREngine.PP_STRUCTURE_V3,
                model_versions=model_versions,
                state=SecondaryResultState.VALID,
                text_origin=None,
            )
        assert plain_text is not None
        assert origin is not None
        return _plain_text_result(
            kind=kind,
            origin=origin,
            angle=angle,
            content=plain_text,
            confidence=min(
                max(
                    (score for _, score in high_confidence),
                    default=0.0,
                ),
                text_confidence,
            ),
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


def _recognized_plain_text(
    overall_ocr: object,
    policy: _TextRecognitionPolicy,
) -> tuple[str | None, int, int, float]:
    if not isinstance(overall_ocr, Mapping):
        raise ValueError
    texts = overall_ocr.get("rec_texts")
    scores = overall_ocr.get("rec_scores")
    boxes = overall_ocr.get("rec_boxes")
    if (
        not isinstance(texts, list)
        or not isinstance(scores, list)
        or not isinstance(boxes, list)
        or len(texts) != len(scores)
        or len(texts) != len(boxes)
        or len(texts) > policy.max_lines
    ):
        raise ValueError

    retained_lines: list[str] = []
    retained_scores: list[float] = []
    for raw_text, raw_score, raw_box in zip(
        texts,
        scores,
        boxes,
        strict=True,
    ):
        if not isinstance(raw_text, str) or not _finite_score(raw_score):
            raise ValueError
        raw_text.encode("utf-8", errors="strict")
        if (
            not isinstance(raw_box, list)
            or len(raw_box) != 4
            or any(not _finite_coordinate(value) for value in raw_box)
        ):
            raise ValueError
        x0, y0, x1, y1 = (float(value) for value in raw_box)
        if x0 > x1 or y0 > y1:
            raise ValueError

        line = raw_text.strip()
        score = float(raw_score)
        if not line or score < policy.recognition_threshold:
            continue
        retained_lines.append(line)
        retained_scores.append(score)

    if not retained_lines:
        return None, 0, 0, 0.0
    text = "\n".join(retained_lines)
    encoded = text.encode("utf-8", errors="strict")
    if (
        len(retained_lines) > policy.max_lines
        or len(text) > policy.max_characters
        or len(encoded) > policy.max_utf8_bytes
    ):
        raise ValueError
    character_count = sum(
        1 for character in text if not character.isspace()
    )
    return (
        text,
        len(retained_lines),
        character_count,
        min(retained_scores),
    )


def _plain_text_result(
    *,
    kind: SecondaryResultKind,
    origin: SecondaryTextOrigin,
    angle: OrthogonalAngle,
    content: str,
    confidence: float,
    model_versions: Mapping[str, str],
) -> SecondaryOcrResult:
    return SecondaryOcrResult(
        kind=kind,
        angle=angle,
        content=content,
        content_format=SecondaryContentFormat.PLAIN_TEXT,
        confidence=confidence,
        engine=SecondaryOCREngine.PP_STRUCTURE_V3,
        model_versions=model_versions,
        state=SecondaryResultState.VALID,
        text_origin=origin,
    )


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


def _finite_coordinate(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
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
        text_origin=None,
    )


def _safe_orientation_result(model_version: str) -> OrientationClassificationResult:
    return OrientationClassificationResult(
        angle=OrthogonalAngle.DEG_0,
        confidence=0.0,
        model_version=model_version,
    )
