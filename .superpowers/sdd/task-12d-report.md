# Task 12D implementation report

## Status

Implemented queue/task/stage instrumentation for orchestration, waiting-queue
instrumentation for the single-owner Paddle worker, durable once-only terminal
outcomes for orientation recovery, fault-isolation/security coverage, and updated
operator documentation. Docker liveness remains unchanged on unauthenticated
`/health/live` using Python's standard library. No push, sync, image build, or
deployment was performed.

## RED/GREEN evidence

### Orchestration queue

- RED command:
  `.venv\Scripts\python.exe -m pytest tests/test_orchestration.py::test_wake_queue_gauge_uses_replacement_semantics_and_close_drains -q`
- RED: `TypeError: OrchestrationService.__init__() got an unexpected keyword argument 'observability'`.
- GREEN: the same test passed after adding the optional no-op sink, replacement
  gauge updates, and close draining.

### Secondary OCR waiting queue

- RED command:
  `.venv\Scripts\python.exe -m pytest tests/test_secondary_ocr_worker.py::test_worker_queue_gauge_counts_waiters_not_active_or_stop -q`
- RED: `TypeError: SingleOwnerSecondaryOcrWorker.__init__() got an unexpected keyword argument 'observability'`.
- GREEN: the same test passed after adding explicit waiting-job accounting that
  excludes the active job and stop sentinel and reports zero after close/failure.

### Orientation recovery terminal outcomes

- RED command:
  `.venv\Scripts\python.exe -m pytest tests/test_orientation_recovery.py::test_recovery_observes_new_completion_once_but_not_completed_replay -q`
- RED: `TypeError: OrientationRecoveryCoordinator.__init__() got an unexpected keyword argument 'observability'`.
- GREEN: the same test passed with best-effort finite outcome recording and replay
  suppression.
- Durable RED command:
  `.venv\Scripts\python.exe -m pytest tests/test_orientation_repository.py::test_concurrent_restart_reconciliation_converges_without_running_work -q`
- Durable RED: expected `[RecoveryOutcome.COMPLETED]`, received `[]`.
- Durable GREEN: passed after adding the atomic SQLite `terminal_observed` marker;
  concurrent reconciliation reserves and emits one outcome only.

### Shutdown race and documentation regressions

- An expanded orchestration test exposed that shutdown could cancel a worker after
  terminal persistence but before attempt observation. Moving terminal
  classification before notification and observation before awaited cleanup made
  the test pass with one warning attempt and de-duplicated stage intervals.
- Full-suite RED found README compatibility assertions for `服务层` and `仓库骨架`.
  Both were restored as accurate non-skeleton claims; the focused regression test
  then passed (`1 passed`).

## Implementation notes

- Orchestration uses only the injected monotonic clock. Queue gauges read `qsize()`
  without consuming/reordering work. Stage transitions are recorded only after an
  accepted repository transition; duplicate same-stage reports do not close or
  restart intervals. Every claim path receives a finite task/stage terminal class.
- Paddle maintains `_waiting_jobs` under its existing re-entrant lock. Observation
  exceptions are contained by `best_effort`; the active job and `_STOP` never
  contribute to public or metric depth.
- Recovery terminal accounting is reserved after the terminal SQLite transaction
  commits, so metric calls are outside transactions. Completed replay and concurrent
  restart reconciliation cannot increment twice. Metric labels receive only finite
  enums; no IDs, pages, angles, engine values, tokens, exception objects, or text.
- `tests/test_observability_faults.py` verifies raising sinks cannot alter queue
  lifecycle/saturation and production Python surfaces contain no fault-injection
  control names. Existing REST/MCP, safe logging, readiness, canary, and exact
  three-tool tests were retained in the required focused run.

## Verification evidence

- Required focused suite:
  `212 passed` (exit 0).
- Expanded focused suite including orientation repository:
  `239 passed` (exit 0).
- Final full suite with isolated controller-safe basetemp:
  `.venv\Scripts\python.exe -m pytest -o addopts='' --basetemp=.pytest-tmp/task12d-full-final3 -q`
  -> `963 passed, 20 skipped in 18.39s` (983 collected, exit 0).
- `.venv\Scripts\python.exe -m pip check` -> `No broken requirements found.`
- `.venv\Scripts\python.exe -m compileall -q src tests` -> exit 0.
- `git diff --check` -> exit 0; only informational Windows LF/CRLF warnings.
- Initial full-suite attempts: one five-minute harness timeout with no result, then
  two completed runs each exposed the README assertions above (`962 passed,
  20 skipped, 1 failed`). These were genuine failures and were resolved before the
  final green run.

## Self-review and concerns

- Reviewed all changed call sites for transaction boundaries, label cardinality,
  queue consumption/order, cancellation, idempotent replay, migration defaults,
  and accidental secret/error propagation.
- The durable marker provides at-most-once emission across restarts. If a metrics
  sink itself raises after the marker is committed, recovery state remains correct
  and replay does not duplicate; as with in-process Prometheus metrics generally,
  that single failed observation is intentionally not replayed.
- Skips are the repository's existing platform/optional-dependency skips; no new
  skip or xfail was added.
- No unresolved implementation concern or delivery-gate action remains.

## Review remediation

The first Task 12D review was NOT READY with four Important findings. All were
handled in strict additional RED/GREEN cycles.

### Claim lifecycle guard

- Added deterministic cancellation during the initial notification publish and
  while yielding behind a duplicate active file.
- RED: both tests persisted the correct `processing` business state but recorded
  zero task/stage terminal observations.
- GREEN: the entire post-claim path now shares one `try/finally`; both tests record
  exactly one `cancelled` task and one closed stage interval without a false
  business terminal write.

### Nested repository and heartbeat classification

- Added six nested handler cases covering `retry_or_fail` and `fail_file` raising
  lease conflict, persistence failure, and unexpected failure.
- Added heartbeat lease-loss and persistence-failure outcome assertions.
- RED: all eight cases were classified as `cancelled` because sibling handlers do
  not catch exceptions raised inside another handler and heartbeat returned no
  reason.
- GREEN: nested repository writes classify before re-raising; heartbeat returns a
  finite reason to execution. Lease conflicts are `lease_conflict`, persistence
  and unknown dependency faults are `unexpected_failure`, and business state is
  left for lease recovery.

### Required durable recovery marker

- Removed `getattr` and the process-memory `_observed_claims` fallback. The
  coordinator now invokes the required protocol method directly and contains
  missing/raising behavior without changing the recovery result.
- Updated recovery fakes to implement the durable marker contract.
- RED: a missing marker incorrectly emitted `completed` through memory fallback;
  a malformed truthy return also incorrectly emitted `completed`.
- GREEN: missing/broken/malformed marker paths emit nothing and do not fail the
  business operation; only literal `True` reserves an outcome. A newly constructed
  coordinator replaying the same durable terminal record does not emit again.

### Expanded fault isolation

- Expanded `tests/test_observability_faults.py` from two tests to nine bounded,
  deterministic tests. Coverage now includes raising and synchronous-costly sinks
  at claimed task/stage outcomes; Paddle active/dequeue/FIFO/cancel/saturation/
  backend-failure/close paths; recovery terminal restart/replay; REST and MCP auth
  response equivalence; exact three-tool/recovery-schema gates; canary exclusion;
  and absence of production fault controls.
- The expanded fault file passes `9 passed`; affected orchestration, Paddle,
  recovery, repository, and fault suites pass `113 passed`.
- Post-review required focused suite passes `230 passed in 6.74s`.

### Post-review final verification

- Expanded focused suite, including the durable orientation repository:
  `257 passed in 9.04s` using isolated basetemp
  `.pytest-tmp/task12d-review-expanded-isolate`.
- Full suite: `981 passed, 20 skipped in 18.41s` (1001 total) using isolated
  basetemp `.pytest-tmp/task12d-review-full-isolate`.
- `.venv\Scripts\python.exe -m pip check` -> `No broken requirements found.`
- `.venv\Scripts\python.exe -m compileall -q src tests` -> exit 0.
- `git diff --check` -> exit 0 with informational Windows LF/CRLF notices only.
- One combined PowerShell command that chained expanded, full, and ancillary checks
  produced no output for more than 150 seconds and was terminated without being
  treated as evidence. The expanded and full commands were immediately isolated
  with separate basetemps; both completed in their normal windows with the exact
  green results above. No test-process hang reproduced.

No production fault control, push, sync, image build, or deployment was performed.
