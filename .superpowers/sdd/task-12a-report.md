# Task 12A report

## Status

Implemented the transport-neutral finite observability contract and isolated
Prometheus registry adapter only. No HTTP middleware, endpoint, health probe,
logging, worker/orchestration/recovery instrumentation, push, or deployment was
performed.

## TDD evidence

### RED

Command:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_observability.py -q
```

Result: exit 1 during collection, as expected. The failure was
`ModuleNotFoundError: No module named
'ocr_mcp_server.infra.prometheus_observability'`.

### GREEN

Command:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_observability.py -q
```

The first implementation run had 26 passing tests and one failing test because
the test helper looked for the HTTP histogram sum without its required
`method`/`route` labels. The assertion was corrected without changing
production behavior.

Fresh result after correction: exit 0, 27 passed.

## Verification evidence

Command:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_observability.py tests/test_repository.py tests/test_docker_assets.py -q
```

Result: exit 0, 46 passed.

Command:

```powershell
.venv\Scripts\python.exe -m pip check
```

Result: exit 0, `No broken requirements found.`

Command:

```powershell
.venv\Scripts\python.exe -m compileall -q src tests
```

Result: exit 0 with no output.

Command:

```powershell
git diff --check
```

Result: exit 0 with no whitespace errors.

## Self-review

- All required metric names and label sets are fixed in the adapter.
- Every collector uses the caller-provided `CollectorRegistry`; no default
  registry is referenced.
- Counters and histograms accumulate, while gauges use replacement `set` calls.
- Runtime validation rejects booleans, negative/non-finite durations, negative
  or non-integer queue depths, untyped enum labels, arbitrary event objects,
  mutable/unsafe route allowlists, and invalid status classes.
- Unknown safe HTTP method tokens normalize to `OTHER`; caller paths absent
  from the injected frozen route allowlist normalize to `unmatched`.
- The shared `best_effort` helper catches only `Exception`, so
  `KeyboardInterrupt` and `SystemExit` propagate, and exception details are not
  returned or serialized.
- Sensitive canaries are absent from Prometheus exposition.
- The direct runtime dependency is exactly `prometheus-client>=0.21,<1`, and
  repository/container dependency contract tests were updated narrowly.

## Concerns

The brief requires fixed buckets and finite enum outcomes but does not state
their exact members/cutoffs. The implementation derives task/recovery outcomes
from the explicitly named Task 12D scenarios, uses the existing
`ProcessingStage` enum for stage labels, and exports one conventional fixed
duration bucket tuple:
`(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)`.

## Review follow-up

Two Important review findings were addressed with focused tests before the
production bucket change.

### Follow-up RED

Command:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_observability.py -q
```

Result: exit 1 during collection because the required split constants did not
exist: `ImportError: cannot import name 'HTTP_DURATION_BUCKETS' from
'ocr_mcp_server.infra.prometheus_observability'`. This was the expected failure
for the single short bucket tuple shared by HTTP, task, and stage histograms.

### Follow-up GREEN

Command:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_observability.py -q
```

Result: exit 0, 34 passed. HTTP buckets remain fixed through 10 seconds. Task
and stage buckets use a separate fixed OCR-duration tuple through 900 seconds.
The tests observe 30, 120, 300, 600, and 900 seconds and verify exact cumulative
bucket counts rather than only comparing tuple reuse.

The canary-coverage finding was a test defect rather than a production behavior
defect: once the missing bucket exports were implemented, the new parameterized
cases passed against the existing route normalization. Eight distinct cases now
pass an ID, filename, URL, Windows path, Unix path, token, OCR text, and
exception text through the existing `HttpObservation.route` boundary, assert
each becomes `unmatched`, record it, and assert that exact canary is absent from
the generated exposition. No production free-text parameter was added.

### Follow-up concerns

None. The earlier bucket concern is resolved by explicit exported tuples for
HTTP and OCR work. The finite outcome members remain derived from the approved
Task 12D scenarios as previously documented.
