# Task 12 Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add low-cardinality Prometheus metrics, content-free JSON logging, strict dependency readiness, and test-only fault injection to `ocr-gateway`.

**Architecture:** A transport-neutral observability contract owns finite enums and best-effort events. A Prometheus adapter uses an injected registry; an ASGI middleware maps requests to route templates without reading bodies. A readiness service runs injected SQLite, MinerU, and selected Paddle probes concurrently with per-probe timeouts. Production exposes only `/health/live`, `/health/ready`, and `/metrics`; all fault injection remains constructor-only test doubles.

**Tech Stack:** Python 3.11, FastAPI/ASGI, prometheus-client, asyncio, standard-library logging/JSON, pytest/pytest-asyncio.

## Global Constraints

- `/health/live`, `/health/ready`, and `/metrics` are unauthenticated; all other REST and MCP paths retain existing API-key authentication.
- `/health/live` performs no database, network, model, queue, or filesystem check.
- `/health/ready` returns `503` when SQLite, MinerU, or the deployment-selected Paddle worker is unavailable, times out, or returns an invalid result.
- Metrics and logs contain no OCR text, document bytes, filename, URL, filesystem path, API key, authorization header, raw recovery token, or exception text.
- Metric labels are finite and low-cardinality; opaque batch/file/recovery/artifact IDs are never metric labels.
- `/metrics` does not record its own scrape as an HTTP request metric.
- Observability failures never change task state, idempotency, retry policy, result bytes, or HTTP business responses.
- Fault injection is constructor-only and test-only. Do not add a REST route, MCP tool, YAML setting, environment variable, or CLI flag for it.
- MCP still exposes exactly `parse_documents`, `get_task_status`, and `reparse_with_page_orientation`.
- Do not add Redis, PostgreSQL, an external queue, OpenTelemetry, PaddleOCR-VL, or runtime engine selection.

---

### Task 12A: Finite observability contracts and Prometheus registry

**Files:**

- Create: `src/ocr_mcp_server/services/observability.py`
- Create: `src/ocr_mcp_server/infra/prometheus_observability.py`
- Modify: `src/ocr_mcp_server/services/__init__.py`
- Modify: `pyproject.toml`
- Create: `tests/test_observability.py`
- Modify: `tests/test_repository.py`
- Modify: `tests/test_docker_assets.py`

**Interfaces:**

- Produces `HttpObservation(method, route, status_class, duration_seconds)` with finite normalized values.
- Produces `TaskOutcome`, `StageOutcome`, and `RecoveryOutcome` enums.
- Produces synchronous `ObservabilitySink` methods: `observe_http`, `observe_task`, `observe_stage`, `observe_recovery`, `set_orchestration_queue_depth`, `set_secondary_ocr_queue_depth`, and `set_dependency_ready`.
- Produces `NullObservability` and `PrometheusObservability(registry: CollectorRegistry)`.

- [ ] **Step 1: Write failing contract tests**

Add tests proving booleans, negative/non-finite durations, arbitrary method/route/status/stage/outcome strings, IDs, URLs and free-form labels are rejected before reaching an adapter. Assert `NullObservability` accepts only valid typed events and never raises for valid input.

- [ ] **Step 2: Run the new tests and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_observability.py -q`

Expected: collection fails because `services.observability` does not exist.

- [ ] **Step 3: Implement the finite contracts**

Use frozen slot dataclasses and `StrEnum`. Normalize HTTP methods to `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, `OPTIONS`, or `OTHER`; status classes to `2xx`, `3xx`, `4xx`, or `5xx`; routes must come from an injected frozen allowlist plus `unmatched`. Do not accept arbitrary label dictionaries or exception objects.

- [ ] **Step 4: Write failing Prometheus tests**

With a fresh `CollectorRegistry` per test, assert exact metric families:

```text
ocr_http_requests_total{method,route,status_class}
ocr_http_request_duration_seconds{method,route}
ocr_tasks_total{outcome}
ocr_task_duration_seconds{outcome}
ocr_pipeline_stage_duration_seconds{stage,outcome}
ocr_orchestration_queue_depth
ocr_secondary_ocr_queue_depth
ocr_dependency_ready{dependency}
ocr_recovery_total{outcome}
```

Verify gauges replace values, counters accumulate, histograms use fixed buckets, two registries do not leak state, and the exposition contains none of the injected sensitive canaries.

- [ ] **Step 5: Add the direct dependency and minimal adapter**

Add `prometheus-client>=0.21,<1` as a direct runtime dependency. Implement only the fixed metric set above. Catch adapter failures at call sites through a shared `best_effort(observation: Callable[[], None])` helper that re-raises `KeyboardInterrupt`/`SystemExit` but otherwise returns without exposing the cause.

- [ ] **Step 6: Verify and commit**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_observability.py tests/test_repository.py tests/test_docker_assets.py -q
.venv\Scripts\python.exe -m pip check
git diff --check
```

Commit: `feat: add bounded prometheus observability`

---

### Task 12B: Safe JSON logs and HTTP observation boundary

**Files:**

- Create: `src/ocr_mcp_server/infra/safe_logging.py`
- Create: `src/ocr_mcp_server/api/observability.py`
- Modify: `src/ocr_mcp_server/app.py`
- Modify: `src/ocr_mcp_server/api/auth.py`
- Create: `tests/test_safe_logging.py`
- Create: `tests/test_http_observability.py`
- Modify: `tests/test_api_auth.py`
- Modify: `tests/test_rest_api.py`
- Modify: `tests/test_mcp_api.py`

**Interfaces:**

- Produces `SafeLogEvent` with only `event`, optional server-generated opaque IDs, stage, stable error code, duration milliseconds, and bounded integer counts.
- Produces `JsonEventFormatter` and `SafeEventLogger.emit(event: SafeLogEvent)`; neither accepts `exc_info`, arbitrary `extra`, or free text.
- Produces `HttpObservabilityMiddleware(app, sink, logger, route_templates)`.
- Produces unauthenticated `GET /metrics` backed by the app's injected registry.

- [ ] **Step 1: Write failing log-safety tests**

Inject canaries representing OCR text, filename, URL, Windows/Linux paths, API key, Authorization header, recovery token and exception text. Assert invalid event construction fails content-free and valid JSON contains only the documented keys. Force the underlying logging handler to throw and assert `SafeEventLogger.emit` returns without recursion or business failure.

- [ ] **Step 2: Confirm RED, then implement the safe logger**

Run: `.venv\Scripts\python.exe -m pytest tests/test_safe_logging.py -q`

Expected: missing module. Implement a one-line JSON formatter with UTC timestamp, finite field names and stable serialization; do not serialize `LogRecord.args`, `msg`, `exc_info`, stack info or arbitrary attributes.

- [ ] **Step 3: Write failing ASGI middleware tests**

Prove route templates, not raw paths, are labels; unknown paths become `unmatched`; `/metrics` is excluded; 2xx/3xx/4xx/5xx are counted; exceptions remain handled by existing safe boundaries; streaming MCP bodies are not buffered; cancellation is re-raised; a failing sink/logger does not change response status or body.

- [ ] **Step 4: Implement HTTP middleware and metrics endpoint**

Resolve the route template only after routing has populated `scope["route"]`; otherwise use `unmatched`. Measure with an injected monotonic clock. Count once after the final response-start status is known. Render `generate_latest(registry)` as the Prometheus content type without logging or echoing request data.

- [ ] **Step 5: Update auth exclusions narrowly**

Replace the single path exception with the exact frozen set `{ "/health/live", "/health/ready", "/metrics" }`. Add tests that prefix/suffix variants, query tricks and `/mcp` remain protected and fail closed when no API key is configured.

- [ ] **Step 6: Verify exactly three MCP tools and commit**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_safe_logging.py tests/test_http_observability.py tests/test_api_auth.py tests/test_rest_api.py tests/test_mcp_api.py -q
git diff --check
```

Commit: `feat: expose content-free http observability`

---

### Task 12C: Strict concurrent readiness probes

**Files:**

- Create: `src/ocr_mcp_server/services/health.py`
- Create: `src/ocr_mcp_server/infra/health_probes.py`
- Create: `src/ocr_mcp_server/api/health.py`
- Modify: `src/ocr_mcp_server/api/__init__.py`
- Modify: `src/ocr_mcp_server/app.py`
- Modify: `src/ocr_mcp_server/settings.py`
- Modify: `config/example.yaml`
- Create: `tests/test_health.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_settings.py`

**Interfaces:**

- Produces `DependencyName` enum with exactly `sqlite`, `mineru`, and `paddle`.
- Produces `ProbeResult(dependency, status, code)` where status is `ready` or `unavailable` and code is a finite enum.
- Produces async `DependencyProbe.check() -> ProbeResult` and `ReadinessService.check() -> ReadinessSnapshot`.
- `create_app(..., readiness: ReadinessService | None = None)` uses a default unavailable snapshot until real dependencies are composed; Task 13 will inject live probes.

- [ ] **Step 1: Write failing readiness service tests**

Use controlled probes/events to prove all three checks start before any completes, results are deterministically ordered, each timeout is bounded, exceptions and malformed results become `unavailable`, cancellation propagates, and no exception text appears in snapshots or representations.

- [ ] **Step 2: Confirm RED, then implement the aggregator**

Run: `.venv\Scripts\python.exe -m pytest tests/test_health.py -q`

Expected: missing service module. Use `asyncio.TaskGroup` or explicit tasks plus `asyncio.timeout`; cancel and await remaining tasks on caller cancellation. Update `ocr_dependency_ready` best-effort after each snapshot.

- [ ] **Step 3: Add concrete narrow probes**

SQLite executes `SELECT 1` through an injected async callable/session factory. MinerU invokes an injected no-argument health callable that does not submit work or expose its URL. Paddle reads an injected worker readiness view (`lifecycle == running`, owner thread alive, not closing); it must never start a worker, load a model, switch engines or dequeue work.

- [ ] **Step 4: Add readiness configuration**

Add a bounded positive `health.probe_timeout_seconds` defaulting to `3.0`. Reject booleans, non-finite values and contradictory configuration. Document the value in `config/example.yaml`; do not add fault-injection settings.

- [ ] **Step 5: Implement `/health/ready`**

Return `200` with `{ "status": "ready", "dependencies": [...] }` only when all three probes are ready. Otherwise return `503` with `{ "status": "unavailable", "dependencies": [...] }`. The endpoint and logs must not include URLs, hosts, ports, exception names/messages, paths or credentials. Preserve `/health/live` as the existing constant response with no probe call.

- [ ] **Step 6: Verify and commit**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_health.py tests/test_app.py tests/test_api_auth.py tests/test_settings.py -q
git diff --check
```

Commit: `feat: add strict dependency readiness`

---

### Task 12D: Queue/task instrumentation, fault isolation, and delivery gates

**Files:**

- Modify: `src/ocr_mcp_server/services/orchestration.py`
- Modify: `src/ocr_mcp_server/infra/secondary_ocr.py`
- Modify: `src/ocr_mcp_server/services/orientation_recovery.py`
- Modify: `src/ocr_mcp_server/app.py`
- Modify: `docker/ocr-gateway.Dockerfile`
- Modify: `README.md`
- Modify: `tests/test_orchestration.py`
- Modify: `tests/test_secondary_ocr_worker.py`
- Modify: `tests/test_orientation_recovery.py`
- Modify: `tests/test_docker_assets.py`
- Create: `tests/test_observability_faults.py`
- Append: `.superpowers/sdd/task-12-report.md`
- Update after deployment only: `.superpowers/sdd/progress.md`

**Interfaces:**

- `OrchestrationService(..., observability: ObservabilitySink | None = None)` records task/stage duration and current wake queue depth.
- `SingleOwnerSecondaryOcrWorker(..., observability: ObservabilitySink | None = None)` records current waiting queue depth without counting the active job and without consuming queue entries.
- `OrientationRecoveryCoordinator(..., observability: ObservabilitySink | None = None)` records one terminal recovery outcome per canonical claim.

- [ ] **Step 1: Write failing orchestration instrumentation tests**

Use a fake monotonic clock and recording sink. Cover completed, warning, retry, terminal failure, unexpected failure, lease conflict and cancellation. Assert one task outcome per actual attempt, one stage duration per observed stage transition, and exact queue Gauge changes on enqueue/dequeue/close. Assert duplicate wake signals and replay do not double-count durable task completions.

- [ ] **Step 2: Implement best-effort orchestration observations**

Never place observation calls inside SQLite transactions. Measure from claim execution start and progress stage boundaries. Wrap every sink call with the shared best-effort helper. Do not pass notification objects, IDs, exception objects or error messages into the sink.

- [ ] **Step 3: Write and implement worker/recovery instrumentation tests**

Verify Paddle queue depth is current through saturation, cancellation, close rejection, failed startup and thread exit. Verify orientation outcomes distinguish completed, uncertain, conflict, invalid token, unavailable and failed without token/page/file labels. Identical completed replay must not increment a second completed recovery.

- [ ] **Step 4: Add fault-isolation tests**

Create raising sinks, raising log handlers, slow/raising/malformed probes and a failing metrics renderer entirely in tests. Assert business REST/MCP responses, task state transitions, artifact bytes and idempotency match the no-fault baseline. Assert there is no production fault route, setting, environment key or fourth MCP tool.

- [ ] **Step 5: Update Docker and operator documentation**

Keep Docker `HEALTHCHECK` on `/health/live`; document that traffic routing must use `/health/ready`, and Prometheus scrapes unauthenticated `/metrics` only on a trusted network. Document all metric names and the prohibition on business-content labels/logs. Do not add heavy OCR dependencies to the gateway image.

- [ ] **Step 6: Run focused and complete local verification**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_observability.py tests/test_safe_logging.py tests/test_http_observability.py tests/test_health.py tests/test_observability_faults.py tests/test_orchestration.py tests/test_secondary_ocr_worker.py tests/test_orientation_recovery.py tests/test_rest_api.py tests/test_mcp_api.py tests/test_docker_assets.py -q
.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-controller-task12
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m compileall -q src tests
git diff --check
```

- [ ] **Step 7: Request independent review and close findings**

Review token secrecy, label cardinality, route-template handling, streaming behavior, cancellation, readiness concurrency/timeouts, metrics/log fault isolation, queue accounting, exactly three MCP tools, and absence of production fault injection. Re-run affected tests after every Important/Critical fix.

- [ ] **Step 8: Commit, push, deploy, and pin**

Commit: `feat: complete task 12 observability`

After controller review is READY, follow the mandated cadence exactly:

1. Run the fresh complete Windows suite and safety checks.
2. Push `feat/repository-skeleton` to GitHub.
3. Create a complete git bundle, verify SHA-256, and fast-forward `/home/jiangren/ocr-mcp-server` on Ubuntu.
4. Run the complete Linux suite in the test dependency image with `/workspace/src` first on `PYTHONPATH` and an isolated temp directory.
5. Build `ocr-mcp-server:0.1.0-<commit>-candidate` on Ubuntu.
6. Immediately start a new candidate container on a new localhost port and verify liveness, strict readiness response, unauthenticated metrics, authenticated REST, exactly three MCP tools, non-root runtime and `pip check`.
7. Only after those checks pass, tag `ocr-mcp-server:0.1.0-<commit>` and write a content-free deployment record.
8. Mark Task 12 complete in the parent plan and `.superpowers/sdd/progress.md` only after the immutable image is pinned.

## Acceptance evidence

- `/metrics` is unauthenticated, scrapeable, low-cardinality and content-free.
- HTTP success rate, request/task/stage duration, orchestration/Paddle queue depth, dependency readiness and orientation recovery outcomes are observable.
- `/health/live` remains constant and dependency-free.
- `/health/ready` checks exactly SQLite, MinerU and selected Paddle concurrently and returns stable `503` on any failure.
- Logs are one-line JSON with only the approved fields and no business content or secrets.
- Injected observability failures do not alter business results or durable state.
- No production fault-injection surface exists.
- REST authentication remains fail closed and MCP still exposes exactly three tools.
