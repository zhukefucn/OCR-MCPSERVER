from __future__ import annotations

import math

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from ocr_mcp_server.domain.models import ProcessingStage
from ocr_mcp_server.infra.prometheus_observability import (
    HTTP_DURATION_BUCKETS,
    OCR_DURATION_BUCKETS,
    PrometheusObservability,
)
from ocr_mcp_server.services.observability import (
    DependencyName,
    HttpObservation,
    NullObservability,
    RecoveryOutcome,
    StageOutcome,
    TaskOutcome,
    best_effort,
)


ROUTES = frozenset({"/health/live", "/tasks/{task_id}"})


def _http(
    *,
    method: str = "GET",
    route: str = "/health/live",
    status_class: str = "2xx",
    duration_seconds: float = 0.25,
) -> HttpObservation:
    return HttpObservation(
        method,
        route,
        status_class,
        duration_seconds,
        route_allowlist=ROUTES,
    )


def test_http_observation_is_frozen_slotted_and_normalizes_finite_labels() -> None:
    observation = _http(method="post", route="/not/a/template")

    assert observation.method == "POST"
    assert observation.route == "unmatched"
    assert observation.status_class == "2xx"
    assert not hasattr(observation, "__dict__")
    with pytest.raises((AttributeError, TypeError)):
        observation.route = "/changed"  # type: ignore[misc]


@pytest.mark.parametrize("method", ["TRACE", "CONNECT", "BREW"])
def test_http_observation_normalizes_other_safe_http_tokens(method: str) -> None:
    assert _http(method=method).method == "OTHER"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("method", "GET /secret"),
        ("method", "https://secret.example"),
        ("route", True),
        ("route", {"route": "/health/live"}),
        ("status_class", "200"),
        ("status_class", "6xx"),
        ("status_class", RuntimeError("secret")),
        ("duration_seconds", True),
        ("duration_seconds", -0.01),
        ("duration_seconds", math.inf),
        ("duration_seconds", math.nan),
        ("duration_seconds", RuntimeError("secret")),
    ],
)
def test_http_observation_rejects_unbounded_or_invalid_values(
    field: str, value: object
) -> None:
    arguments: dict[str, object] = {
        "method": "GET",
        "route": "/health/live",
        "status_class": "2xx",
        "duration_seconds": 0.1,
        "route_allowlist": ROUTES,
    }
    arguments[field] = value

    with pytest.raises((TypeError, ValueError), match="^invalid observation$"):
        HttpObservation(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "route_allowlist",
    [set(ROUTES), frozenset({"/raw/123"}), frozenset({"https://secret.example"})],
)
def test_http_observation_requires_a_frozen_template_allowlist(
    route_allowlist: object,
) -> None:
    with pytest.raises((TypeError, ValueError), match="^invalid observation$"):
        HttpObservation(
            "GET",
            "/health/live",
            "2xx",
            0.1,
            route_allowlist=route_allowlist,  # type: ignore[arg-type]
        )


def test_null_observability_accepts_only_typed_bounded_values() -> None:
    sink = NullObservability()

    sink.observe_http(_http())
    sink.observe_task(TaskOutcome.COMPLETED, 1.25)
    sink.observe_stage(ProcessingStage.MERGING, StageOutcome.COMPLETED, 0.5)
    sink.observe_recovery(RecoveryOutcome.UNCERTAIN)
    sink.set_orchestration_queue_depth(3)
    sink.set_secondary_ocr_queue_depth(2)
    sink.set_dependency_ready(DependencyName.SQLITE, True)

    invalid_calls = (
        lambda: sink.observe_http({"route": "/health/live"}),
        lambda: sink.observe_task("completed", 1.0),
        lambda: sink.observe_task(TaskOutcome.COMPLETED, False),
        lambda: sink.observe_stage("merging", StageOutcome.COMPLETED, 1.0),
        lambda: sink.observe_stage(ProcessingStage.MERGING, "completed", 1.0),
        lambda: sink.observe_recovery("failed"),
        lambda: sink.set_orchestration_queue_depth(True),
        lambda: sink.set_secondary_ocr_queue_depth(-1),
        lambda: sink.set_dependency_ready("sqlite", True),
        lambda: sink.set_dependency_ready(DependencyName.SQLITE, 1),
    )
    for call in invalid_calls:
        with pytest.raises((TypeError, ValueError), match="^invalid observation$"):
            call()


def test_prometheus_exposes_exact_metric_families_labels_and_accumulation() -> None:
    registry = CollectorRegistry()
    sink = PrometheusObservability(registry)

    sink.observe_http(_http(duration_seconds=0.25))
    sink.observe_http(_http(duration_seconds=0.75))
    sink.observe_task(TaskOutcome.COMPLETED, 1.0)
    sink.observe_task(TaskOutcome.COMPLETED, 2.0)
    sink.observe_stage(ProcessingStage.MERGING, StageOutcome.COMPLETED, 0.5)
    sink.observe_recovery(RecoveryOutcome.COMPLETED)
    sink.observe_recovery(RecoveryOutcome.COMPLETED)
    sink.set_orchestration_queue_depth(7)
    sink.set_orchestration_queue_depth(2)
    sink.set_secondary_ocr_queue_depth(4)
    sink.set_dependency_ready(DependencyName.SQLITE, False)
    sink.set_dependency_ready(DependencyName.SQLITE, True)

    families = {family.name: family for family in registry.collect()}
    assert set(families) == {
        "ocr_http_requests",
        "ocr_http_request_duration_seconds",
        "ocr_tasks",
        "ocr_task_duration_seconds",
        "ocr_pipeline_stage_duration_seconds",
        "ocr_orchestration_queue_depth",
        "ocr_secondary_ocr_queue_depth",
        "ocr_dependency_ready",
        "ocr_recovery",
    }
    assert families["ocr_http_requests"].samples[0].labels == {
        "method": "GET",
        "route": "/health/live",
        "status_class": "2xx",
    }
    assert families["ocr_http_requests"].samples[0].value == 2
    assert _sample(
        families,
        "ocr_http_request_duration_seconds_sum",
        method="GET",
        route="/health/live",
    ) == 1.0
    assert _sample(families, "ocr_tasks_total", outcome="completed") == 2
    assert _sample(families, "ocr_task_duration_seconds_sum", outcome="completed") == 3
    assert _sample(
        families,
        "ocr_pipeline_stage_duration_seconds_sum",
        stage="merging",
        outcome="completed",
    ) == 0.5
    assert _sample(families, "ocr_orchestration_queue_depth") == 2
    assert _sample(families, "ocr_secondary_ocr_queue_depth") == 4
    assert _sample(families, "ocr_dependency_ready", dependency="sqlite") == 1
    assert _sample(families, "ocr_recovery_total", outcome="completed") == 2


def _sample(
    families: dict[str, object], sample_name: str, **labels: str
) -> float:
    for family in families.values():
        for sample in family.samples:  # type: ignore[attr-defined]
            if sample.name == sample_name and sample.labels == labels:
                return float(sample.value)
    raise AssertionError(f"missing sample {sample_name} {labels}")


def test_histograms_use_separate_exact_fixed_buckets_for_http_and_ocr_work() -> None:
    registry = CollectorRegistry()
    sink = PrometheusObservability(registry)
    sink.observe_http(_http())
    long_observations = (30.0, 120.0, 300.0, 600.0, 900.0)
    for duration in long_observations:
        sink.observe_task(TaskOutcome.FAILED, duration)
        sink.observe_stage(
            ProcessingStage.MINERU_PARSING, StageOutcome.COMPLETED, duration
        )

    families = {family.name: family for family in registry.collect()}
    http_buckets = _buckets(
        families["ocr_http_request_duration_seconds"],
        method="GET",
        route="/health/live",
    )
    task_buckets = _buckets(
        families["ocr_task_duration_seconds"], outcome="failed"
    )
    stage_buckets = _buckets(
        families["ocr_pipeline_stage_duration_seconds"],
        stage="mineru_parsing",
        outcome="completed",
    )

    assert HTTP_DURATION_BUCKETS == (
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
    )
    assert OCR_DURATION_BUCKETS == (
        0.1,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
        30.0,
        60.0,
        120.0,
        300.0,
        600.0,
        900.0,
    )
    assert tuple(http_buckets) == (*HTTP_DURATION_BUCKETS, math.inf)
    assert tuple(task_buckets) == (*OCR_DURATION_BUCKETS, math.inf)
    assert tuple(stage_buckets) == (*OCR_DURATION_BUCKETS, math.inf)
    for boundary, expected_count in zip(
        long_observations, range(1, len(long_observations) + 1), strict=True
    ):
        assert task_buckets[boundary] == expected_count
        assert stage_buckets[boundary] == expected_count
    assert task_buckets[math.inf] == len(long_observations)
    assert stage_buckets[math.inf] == len(long_observations)


def _buckets(family: object, **labels: str) -> dict[float, float]:
    return {
        float(sample.labels["le"]): float(sample.value)
        for sample in family.samples  # type: ignore[attr-defined]
        if sample.name.endswith("_bucket")
        and {key: value for key, value in sample.labels.items() if key != "le"}
        == labels
    }


def test_registries_are_isolated() -> None:
    first = CollectorRegistry()
    second = CollectorRegistry()
    first_sink = PrometheusObservability(first)
    PrometheusObservability(second)

    first_sink.observe_recovery(RecoveryOutcome.FAILED)

    assert b'ocr_recovery_total{outcome="failed"} 1.0' in generate_latest(first)
    assert b'ocr_recovery_total{outcome="failed"}' not in generate_latest(second)


@pytest.mark.parametrize(
    "canary",
    [
        pytest.param("batch-123-secret", id="id"),
        pytest.param("invoice-secret.pdf", id="filename"),
        pytest.param("https://secret.example/doc", id="url"),
        pytest.param("C:\\secret\\invoice.pdf", id="windows-path"),
        pytest.param("/srv/secret/invoice.pdf", id="unix-path"),
        pytest.param("token-secret-123", id="token"),
        pytest.param("raw OCR secret text", id="ocr-text"),
        pytest.param("exception-secret", id="exception-text"),
    ],
)
def test_each_sensitive_caller_canary_is_normalized_before_exposition(
    canary: str,
) -> None:
    registry = CollectorRegistry()
    sink = PrometheusObservability(registry)

    observation = HttpObservation(
        "GET",
        canary,
        "4xx",
        0.01,
        route_allowlist=ROUTES,
    )
    assert observation.route == "unmatched"
    sink.observe_http(observation)
    output = generate_latest(registry).decode("utf-8")

    assert canary not in output


def test_best_effort_contains_ordinary_failures_without_exposing_them() -> None:
    def fail() -> None:
        raise RuntimeError("sensitive exception detail")

    assert best_effort(fail) is None


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), SystemExit()])
def test_best_effort_does_not_swallow_process_control(
    interrupt: BaseException,
) -> None:
    def fail() -> None:
        raise interrupt

    with pytest.raises(type(interrupt)):
        best_effort(fail)
