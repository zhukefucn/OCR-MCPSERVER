# Task 8 implementation report

## Status

Implementation status: DONE.

Task 8 adds durable weighted progress, exact counters and ordered events, batch aggregation, bounded local scheduling, lease recovery/heartbeat behavior, a lease-bound pipeline runner contract, safe typed failures, and content-free throttled notifications. No Task 9+ transport, artifact ZIP, retention, engine-runtime, deployment, or endpoint work was added.

## RED/GREEN evidence

All commands used the repository Python 3.11 virtual environment.

### Progress domain

- RED command: `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_progress.py -vv`
- RED result: collection failed with `ModuleNotFoundError: No module named 'ocr_mcp_server.domain.progress'`, the expected missing-contract failure before production code existed.
- GREEN command: `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_progress.py -q`
- GREEN result: `19 passed`.
- Covered exact stage ranges, boundary and integer-floor mapping, unknown/zero totals, Boolean/negative/non-finite/incomplete counters, unknown units/stages, and rejection of counters on queued/terminal stages.

### Durable repository, schema, aggregation, and history

- RED command: `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_task_progress_repository.py -vv`
- RED result: collection failed with `ImportError: cannot import name 'StageEventSnapshot'`, the expected absent durable snapshot/history contract.
- First GREEN diagnostic command: `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_progress.py tests/test_task_progress_repository.py tests/test_task_repository.py tests/test_state_machine.py -q`
- First GREEN diagnostic exposed two intended integration mismatches: a duplicate same-stage counter update was still accepted, and the Task 2 event assertion did not yet include the newly required claim events.
- Fix: reject non-material same-stage counter updates and update the legacy assertion to include durable claim transitions.
- Final GREEN result for the same command: `49 passed`.
- Covered queued 12%, exact counters, monotonic stage/progress/counters, terminal 100 invariants, batch counts/current file/progress/status, ordered immutable file/event snapshots, atomic non-retryable failure, retry/recovery behavior, and safe value validation.

### Settings, orchestration, recovery, and notifications

- RED command: `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_settings.py tests/test_orchestration.py -vv`
- RED result: collection failed with `ModuleNotFoundError: No module named 'ocr_mcp_server.services.orchestration'`, the expected missing transport-independent orchestration boundary. The settings tests had already been authored and collected in the same RED slice.
- Settings GREEN command: `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_settings.py -q`
- Settings GREEN result: `77 passed`.
- Initial orchestration GREEN command: `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_orchestration.py -vv`
- Initial orchestration GREEN result: `7 passed`.
- Requirements-gap coverage then added orchestration retry exhaustion, idle durable polling, periodic recovery, shutdown recovery, and terminal non-reexecution.
- Final orchestration GREEN command: `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_orchestration.py -q`
- Final orchestration GREEN result: `9 passed`.
- Focused integration command: `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_progress.py tests/test_task_progress_repository.py tests/test_task_repository.py tests/test_state_machine.py tests/test_settings.py tests/test_orchestration.py -q`
- Focused integration result: `135 passed`.

No real timing sleeps, GPU, network, MinerU, Paddle, or external service was used. Timing-sensitive tests use an injected manual wall/monotonic clock.

## Files delivered

### New production files

- `src/ocr_mcp_server/domain/progress.py`: fixed ranges, exact counter/unit value, pure integer mapper.
- `src/ocr_mcp_server/services/orchestration.py`: pipeline/cancellation/result/failure/notification contracts and orchestration service.

### Extended production files

- `src/ocr_mcp_server/domain/tasks.py`: counter-aware file snapshot, runtime-aware batch snapshot, immutable event snapshot.
- `src/ocr_mcp_server/domain/__init__.py`: public domain exports.
- `src/ocr_mcp_server/infra/task_models.py`: durable counter, aggregate, current-file, event-version/history columns.
- `src/ocr_mcp_server/infra/task_repository.py`: atomic progress/counter transitions, completion/failure helpers, ordered snapshot reads, monotonic aggregation, claim/retry/recovery events.
- `src/ocr_mcp_server/settings.py`: validated Task 8 lifecycle defaults and bounds.
- `src/ocr_mcp_server/services/__init__.py`: service export.
- `config/example.yaml`: orchestration defaults.

### Tests

- `tests/test_progress.py`
- `tests/test_task_progress_repository.py`
- `tests/test_orchestration.py`
- `tests/test_task_repository.py`
- `tests/test_settings.py`

## Important decisions

- Durable queued rows begin at stage `QUEUED`, weighted progress 12. Claims retain stage/progress and add an ordered committed claim event before notification.
- Exact counters are stored separately from display progress. `None` total means unknown; a zero total is valid only with zero completed. Stage mapping uses integer multiplication/division and cannot exceed the fixed end.
- Repository updates validate enums, reject Boolean progress, bind every processing write to a live lease, and atomically refresh file state, event history, and batch aggregate in one SQLite transaction.
- Batch progress is the floor average of file progress, capped below 100 while any file is nonterminal, and protected from regression. Current file is the lowest-position processing file.
- Retry/recovery returns to `QUEUED` without lowering achieved weighted progress. Exact stage counters are cleared because they are no longer meaningful for the queued stage. Exhaustion transitions directly to safe `FAILED`/100.
- The wake queue holds only content-free tokens. A worker drains durable claims until empty and wakes a peer immediately after a claim, which preserves concurrency even with capacity 1. Queue saturation drops only redundant tokens.
- The SQLite lease remains the ownership boundary. Heartbeat uncertainty or lease conflict cancels the local pipeline task and trips its cooperative cancellation context; subsequent reporter writes are lease rejected.
- Service shutdown never writes customer `CANCELLED`. It stops claims, cancels internal tasks with a one-second upper wait, leaves processing rows to lease expiry, and does not close the injected repository, pipeline, sink, or database engine.
- Notifications are built only from committed immutable snapshots. Stage/status/first-file/terminal changes are immediate; same-stage advances use the injected monotonic clock and configured interval. Tracking is bounded by worker count and removed on terminal state.
- Pipeline errors use a fixed enum and discard causes. Unknown exceptions persist only `pipeline_unexpected`. No exception text, path, filename, OCR text, document bytes, backend body, or manifest is persisted or placed in notifications.

## Commits

- `a99dd67` — `feat: add durable weighted task progress`
- `ddce222` — `feat: orchestrate durable file pipelines`
- The report itself is committed separately after this file is written; its commit is the final Task 8 HEAD reported to the controller.

No push, sync, deployment, image build, parent-plan edit, or external mutation was performed.

## Review closure

- Re-read the Task 8 brief line by line after focused GREEN.
- Confirmed the fixed ranges and UTF-8-decoded limits (`worker_count` 1–8 and wake capacity 1–1024).
- Inspected changed paths and public symbols; changes are confined to Task 8 domain, persistence, settings, orchestration, config documentation, and tests.
- Searched new Task 8 code for forbidden broker/database/endpoint/runtime dependencies and content logging. No Redis, Celery, PostgreSQL, HTTP/MCP endpoint, MinerU/Paddle/Torch/OpenCV import, runtime engine selection, artifact ZIP, or retention cleanup was added.
- Verified snapshots/events/notifications contain only stable identity, enums/codes, numeric counters/progress/version, and timestamps.
- The controller owns independent reviewer dispatch under the team workflow; this implementer performed the required self-review and left no known Critical or Important issue open.

## Verification

Post-implementation, post-commit verification from `ddce222`:

1. `python -m pytest -p no:cacheprovider`
   - Exit 0: `577 passed, 5 skipped in 6.12s`.
   - The five skips are pre-existing optional/runtime-dependent tests; there are no failures or warnings.
2. `python -m pip check`
   - Exit 0: `No broken requirements found.`
3. `python -m compileall -q src tests`
   - Exit 0 with no output.
4. `git diff --check 6364320..HEAD`
   - Exit 0 with no output.

These four commands are rerun once more after the report commit so the controller receives evidence for the final repository HEAD.

## Remaining concerns

- Existing databases are intentionally MVP-local and receive no Alembic migration, exactly as directed. A pre-Task-8 local database must be recreated before using the expanded schema.
- Pipeline implementations must honor the injected cancellation context between their own internal steps. The service also cancels the task and the repository independently rejects stale lease writes, so durable state remains safe even if injected code is slow to cooperate.
- Same-stage throttling intentionally does not retain an unbounded delayed notification backlog. Suppressed intermediate updates remain fully visible through authoritative polling; the next material update after the interval or the next stage/terminal transition is emitted.
