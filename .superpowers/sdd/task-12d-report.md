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

## Final whole-feature review remediation

The final whole-feature review found four additional Important trust-boundary
issues. They were addressed as one coordinated TDD wave.

### Uvicorn and unhandled ASGI failures

- RED: the entrypoint supplied neither `access_log=False` nor a safe logging
  configuration; an injected clock failure before app invocation also returned a
  500 instead of the live business response.
- GREEN: Uvicorn access logging is disabled, its built-in logging configuration is
  disabled, and log level is `critical`, suppressing unsafe request/access/error
  lines. The outer ASGI boundary contains ordinary application exceptions after a
  safe 500 (or sends a fixed content-free 500 if none began). It does not catch
  ASGI cancellation, `KeyboardInterrupt`, or `SystemExit`.
- The existing unhandled exception canary remains a fixed JSON 500 and is absent
  from observation/logger representations. Clock start/end failure tests preserve
  `/health/live` 200 and suppress the exception canary.

### CancelledError containment

- RED: a synchronous sink raising `asyncio.CancelledError` escaped
  `best_effort`.
- GREEN: sink-originated `CancelledError` is contained explicitly while
  `KeyboardInterrupt` and `SystemExit` are re-raised. Real caller task cancellation
  still propagates at await points because dispatcher admission is synchronous and
  does not catch cancellation from application awaits.
- Direct tests and claimed-task/HTTP call-site tests use a synchronous
  `CancelledError` sink and preserve business results.

### Bounded non-blocking dispatch

- RED: no dispatcher type existed; synchronous sinks executed on caller/event-loop
  or Paddle-lock paths.
- GREEN: `ObservationDispatcher` validates finite observation arguments before
  bounded `put_nowait` admission. Defaults are capacity 256 and one daemon worker;
  worker count is exactly one. Full queues and closed dispatchers
  increment a drop counter without blocking business work.
- Sink execution occurs only on fixed worker threads. Ordinary errors and
  sink-originated `CancelledError` are contained. Queue payloads contain only the
  validated finite method arguments, never closures over request/task state.
- `drain(timeout)` supports deterministic fast-sink accounting. `close(timeout)`
  uses one total deadline (default 100 ms), stops admission, and never waits
  indefinitely for a blocked sink. `wait_closed(timeout)` and `alive_workers`
  provide deterministic worker-exit verification after a blocked sink is released.
  App lifespan, orchestration close, and Paddle
  close/startup-failure close owned dispatchers. Readiness and recovery expose
  explicit drain/close hooks. Existing injected dispatchers are reused rather than
  nested, and every app owns an independent queue/registry target.
- Weak dispatcher target references plus owner-lifetime strong references avoid
  retaining readiness services/sinks after their owner is collected.
- Forever-blocking tests prove live/REST/readiness calls, claimed cleanup, Paddle
  FIFO/backend-failure/close, and recovery replay remain bounded. The direct test
  proves pending depth never exceeds capacity, drops occur, worker count remains
  fixed, and close remains bounded. Paddle locks cover only O(1) queue admission,
  never user sink execution.

### Deterministic test updates and final evidence

- Legacy synchronous metric assertions now explicitly drain their owning
  dispatcher where needed; no sleeps were introduced for accounting.
- Component dispatch/fault set: `194 passed`.
- Final Task 12 focused set (including entrypoint and durable orientation
  repository): `267 passed in 11.07s`.
- Full isolated suite before the final entrypoint/Paddle close-order cleanup:
  `990 passed, 20 skipped in 21.06s`; a final post-cleanup full run follows below.
- Final post-cleanup isolated full suite:
  `990 passed, 20 skipped in 20.65s` using basetemp
  `.pytest-tmp/final-review-full-final`.
- Ultimate isolated full suite after deterministic worker-exit verification:
  `990 passed, 20 skipped in 20.47s` using basetemp
  `.pytest-tmp/final-review-full-ultimate`.
- `pip check` reports no broken requirements, `compileall` exits 0, and
  `git diff --check` exits 0 with informational Windows LF/CRLF notices only.

No push, sync, image build, deployment, production fault control, heavy dependency,
or new telemetry backend was added.

## Ubuntu/Linux ordering and ownership gate

The Ubuntu full-suite gate exposed one timing-sensitive assertion and a retained
worker ordering problem that Windows execution order had not made visible.

### Deterministic cancellation duration

- Linux RED: `test_cancel_during_initial_notification_still_closes_claim_observation`
  used the real system clock but asserted an exact zero cancellation duration; the
  observed finite duration was approximately 87 microseconds.
- GREEN: the claim and `OrchestrationService` now share one `ManualClock` in that
  test. Production duration measurement remains unchanged and is not suppressed.

### Lazy app-owned dispatcher activation

- RED: every dispatcher started its worker in the constructor. Eight apps created
  without lifespan produced eight persistent workers, and a TestClient request
  made without entering lifespan also started and retained an app worker.
- GREEN: a dispatcher now creates no thread until it is both active and receives
  its first observation. Generic service-owned dispatchers remain active by
  default. App-owned wrappers are created inactive and activated only at lifespan
  startup; construction and no-lifespan requests therefore create zero workers.
- Activation preserves the exact one-worker FIFO queue. Repeated TestClient
  lifespans start at most one worker per app and leave no app observation workers
  after shutdown. Active owner GC, bounded queue discard, and explicit close use
  the existing identity-safe finalizer path.

### Order-dependent test retention diagnosis

- A per-test worker-count diagnostic found the six retained threads exactly: the
  parametrized terminal repository-fault orchestration cases each retained their
  `pytest.raises` traceback/frame and an unclosed service dispatcher. All 161 broad
  tests completed in about seven seconds, but interpreter shutdown then waited
  with six observation workers still idle.
- Each parametrized case now closes its service in `finally`, including assertion
  failure paths. The same combined order exits normally with
  `161 passed in 7.16s`.

### Final local evidence

- Linux-gate focused Task 12 set: `282 passed in 11.52s` using basetemp
  `.pytest-tmp/linux-gate-focused-final`.
- Complete isolated Windows suite: `1005 passed, 20 skipped in 20.68s` using
  basetemp `.pytest-tmp/linux-gate-full-final`.
- `pip check` reports no broken requirements, `compileall` exits 0, and
  `git diff --check` exits 0 with informational Windows LF/CRLF notices only.
- WSL was unavailable in the local environment; the committed fix is ready for
  the authoritative Ubuntu full-suite rerun.

No push, sync, image build, deployment, production fault control, heavy dependency,
or new telemetry backend was added.

## Final dispatcher ordering, ownership, and scrape remediation

The last dispatcher review identified three lifecycle/concurrency gaps. They were
closed with focused RED/GREEN tests before the final suite.

### FIFO metric application

- RED: constructor values `worker_count=2` and `4` were accepted, allowing a
  delayed first gauge write to be overtaken by a later write.
- GREEN: `worker_count` now accepts only exact integer `1`. A delayed-first gauge
  test proves sink calls remain `[1, 2]` and the final gauge value is the last
  submitted value. Counters and histograms share the same FIFO worker.

### Owner collection and dispatcher shutdown

- RED: the thread target was the bound method `self._run`; an idle unclosed
  dispatcher remained strongly reachable and its worker was still alive after
  repeated GC. Readiness and unstarted app owners collected, but leaked the same
  worker.
- GREEN: the worker runs a module-level function over a private state object with
  no dispatcher back-reference. A per-dispatcher stop-token and
  `weakref.finalize` close only that state, discard its bounded finite-argument
  queue, and wake an idle worker. Explicit `close()` invokes the identical
  identity-safe finalizer path.
- Tests prove idle dispatcher, readiness owner, and unstarted app collection stop
  their workers promptly. A queued-owner test proves the public owner collects
  immediately, queued payloads are discarded, and a worker already inside a
  blocking user sink exits after that sink is released.
- App lifespan closes an injected `ReadinessService` dispatcher only through the
  service's ownership-aware close hook. If readiness received an externally owned
  dispatcher, that hook is a no-op; the app does not double-close it.

### Non-blocking metrics scraping

- RED: three concurrent `/metrics` scrapes called synchronous `drain()` and
  blocked the event loop for about 0.344 seconds behind a forever-blocking sink.
- GREEN: scrape-time draining was removed. Metrics are eventually consistent and
  remain self-excluded. Deterministic accounting tests drain the owning dispatcher
  before issuing a scrape, outside the endpoint.
- The concurrent regression proves repeated unauthenticated scrapes, an unrelated
  live request, and an event-loop tick complete within the bounded threshold while
  the observation sink remains blocked.

### Final evidence

- Dispatcher/health/HTTP component set: `86 passed in 2.40s`; after the queued
  owner addition the dispatcher file alone is `44 passed in 0.66s`.
- Final Task 12 focused set: `278 passed in 10.98s` using basetemp
  `.pytest-tmp/task12-final-focused`.
- Final isolated full suite: `1001 passed, 20 skipped in 21.26s` using basetemp
  `.pytest-tmp/task12-final-full`.
- `pip check` reports no broken requirements, `compileall` exits 0, and
  `git diff --check` exits 0 with informational Windows LF/CRLF notices only.

No push, sync, image build, deployment, production fault control, heavy dependency,
or new telemetry backend was added.
