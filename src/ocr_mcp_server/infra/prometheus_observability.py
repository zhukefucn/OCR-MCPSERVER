"""Prometheus adapter for the finite observability contract."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from ocr_mcp_server.domain.models import ProcessingStage
from ocr_mcp_server.services.observability import (
    DependencyName,
    HttpObservation,
    NullObservability,
    RecoveryOutcome,
    StageOutcome,
    TaskOutcome,
)


HTTP_DURATION_BUCKETS = (
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
OCR_DURATION_BUCKETS = (
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


class PrometheusObservability(NullObservability):
    def __init__(self, registry: CollectorRegistry) -> None:
        if not isinstance(registry, CollectorRegistry):
            raise ValueError("invalid observation")
        self._http_requests = Counter(
            "ocr_http_requests_total",
            "Completed HTTP requests.",
            ("method", "route", "status_class"),
            registry=registry,
        )
        self._http_duration = Histogram(
            "ocr_http_request_duration_seconds",
            "HTTP request duration in seconds.",
            ("method", "route"),
            buckets=HTTP_DURATION_BUCKETS,
            registry=registry,
        )
        self._tasks = Counter(
            "ocr_tasks_total",
            "Task attempts by terminal outcome.",
            ("outcome",),
            registry=registry,
        )
        self._task_duration = Histogram(
            "ocr_task_duration_seconds",
            "Task attempt duration in seconds.",
            ("outcome",),
            buckets=OCR_DURATION_BUCKETS,
            registry=registry,
        )
        self._stage_duration = Histogram(
            "ocr_pipeline_stage_duration_seconds",
            "Pipeline stage duration in seconds.",
            ("stage", "outcome"),
            buckets=OCR_DURATION_BUCKETS,
            registry=registry,
        )
        self._orchestration_queue_depth = Gauge(
            "ocr_orchestration_queue_depth",
            "Current orchestration wake queue depth.",
            registry=registry,
        )
        self._secondary_ocr_queue_depth = Gauge(
            "ocr_secondary_ocr_queue_depth",
            "Current secondary OCR waiting queue depth.",
            registry=registry,
        )
        self._dependency_ready = Gauge(
            "ocr_dependency_ready",
            "Whether a required dependency is ready.",
            ("dependency",),
            registry=registry,
        )
        self._recovery = Counter(
            "ocr_recovery_total",
            "Orientation recovery attempts by outcome.",
            ("outcome",),
            registry=registry,
        )

    def observe_http(self, observation: HttpObservation) -> None:
        super().observe_http(observation)
        self._http_requests.labels(
            observation.method, observation.route, observation.status_class
        ).inc()
        self._http_duration.labels(observation.method, observation.route).observe(
            observation.duration_seconds
        )

    def observe_task(self, outcome: TaskOutcome, duration_seconds: float) -> None:
        super().observe_task(outcome, duration_seconds)
        self._tasks.labels(outcome.value).inc()
        self._task_duration.labels(outcome.value).observe(duration_seconds)

    def observe_stage(
        self,
        stage: ProcessingStage,
        outcome: StageOutcome,
        duration_seconds: float,
    ) -> None:
        super().observe_stage(stage, outcome, duration_seconds)
        self._stage_duration.labels(stage.value, outcome.value).observe(
            duration_seconds
        )

    def observe_recovery(self, outcome: RecoveryOutcome) -> None:
        super().observe_recovery(outcome)
        self._recovery.labels(outcome.value).inc()

    def set_orchestration_queue_depth(self, depth: int) -> None:
        super().set_orchestration_queue_depth(depth)
        self._orchestration_queue_depth.set(depth)

    def set_secondary_ocr_queue_depth(self, depth: int) -> None:
        super().set_secondary_ocr_queue_depth(depth)
        self._secondary_ocr_queue_depth.set(depth)

    def set_dependency_ready(self, dependency: DependencyName, ready: bool) -> None:
        super().set_dependency_ready(dependency, ready)
        self._dependency_ready.labels(dependency.value).set(1 if ready else 0)


__all__ = [
    "HTTP_DURATION_BUCKETS",
    "OCR_DURATION_BUCKETS",
    "PrometheusObservability",
]
