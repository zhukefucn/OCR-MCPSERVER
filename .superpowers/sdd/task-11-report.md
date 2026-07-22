# Task 11 SDD Report

## Task 11D - retention and restart reconciliation

- Added an unconditional `RetentionRepository.invalidate_orientation_recoveries()` gate to `RetentionService`; there is no optional dependency or configuration bypass. It runs while the batch lock is held, after content-write quiescence and before either tombstone is prepared.
- The repository starts `BEGIN IMMEDIATE`, validates the live CONTENT cleanup claim, and atomically marks every non-deleted recovery row for the batch as deleted. Empty batches and retries return zero without changing already-deleted rows. Failure is converted to the stable `cleanup_failed` retention failure and prevents physical deletion.
- Kept retention policy and cleanup-claim ownership in `RetentionRepository`; the standalone orientation repository remains responsible for normal token operations and does not mutate retention policy.
- Added `recovery_id=claim.claim_id` as the required durable idempotency key for full recovery pipeline submission.
- Added runner-side `reconcile(recovery_id)` and a bounded, deterministic SQLite `list_claimed(now, limit)` query. The query returns only claimed records whose recovery and content retention are both still live.
- Added `OrientationRecoveryCoordinator.reconcile_incomplete()`. It only inspects durable runner state and transitions the recovery record; it never invokes the detector, corrector, storage, or pipeline `run` method.
- Existing valid submissions complete at exactly `source_result_version + 1` and must use a distinct result batch. Only an explicit runner `None` proves absence and becomes `orientation_recovery_interrupted`; malformed or foreign results and runner/repository unavailability leave the claim deferred for a later retry.
- Reconciliation output contains counts only: scanned, completed, failed, and deferred.
- Verified SQLite metadata purge removes orientation rows through existing foreign-key cascades.

## TDD evidence

Red tests were introduced first for:

- required runner recovery id;
- invalidation ordering and failure-closed deletion;
- unconditional invalidation with no-row and idempotent retry coverage;
- claimed-row invalidation under a validated CONTENT claim;
- deletion failure after successful token invalidation;
- bounded live claim recovery and expired-content exclusion;
- crash before and after runner submission;
- invalid runner results;
- reconciliation dependency failure;
- cancellation and concurrent reconciliation;
- metadata-purge cascading.

Focused Task 11D suite passed:

```text
python -m pytest -q tests/test_orientation_repository.py tests/test_orientation_recovery.py tests/test_retention.py
........................................................................ [ 77%]
.....................                                                    [100%]
```

The final complete Windows suite passed with `832 passed, 20 skipped`. `pip check`, `compileall -q src tests`, and `git diff --check` also passed. Controller-level review, push, Ubuntu verification, image build/start, and version pinning remain intentionally outside this implementation worker's scope.

## Final Important fixes - durable takeover and batch capacity

### Design and implementation

- `FullRecoveryPipelineSubmission` now carries a content-free durable takeover proof: a new accepted input UUID, SHA-256, and byte size bound to the independent result batch and exact source-result-plus-one version.
- The live coordinator verifies the proof against the immutable corrected input before releasing the source batch lock/content-write guard. SQLite completion additionally verifies that the accepted input task belongs to the result batch and persists the proof atomically.
- Restart reconciliation can recover a claimed operation after token/content expiry or after retention marks the source recovery row deleted. It never repeats detection, correction, or pipeline submission. A runner-owned accepted input can converge to completed while the source token remains unusable.
- Schema initialization includes an additive migration for existing SQLite databases.
- Immutable correction now requires the active matching `BatchLockLease`. While holding that lease and the anchored input-directory handle, storage serializes derivative publication, counts existing immutable inputs plus the staged output, and rejects totals above `DEFAULT_MAX_BATCH_SIZE_BYTES`. The existing 30 MB derivative/file limit remains enforced independently.

### RED evidence

The takeover-proof and capacity tests were added before implementation. The first aggregate run stopped during collection on the missing proof fields:

```text
.venv\\Scripts\\python.exe -m pytest -q tests/test_orientation_recovery.py tests/test_orientation_correction.py -x
ERROR tests/test_orientation_recovery.py
TypeError: FullRecoveryPipelineSubmission.__init__() takes 4 positional arguments but 7 were given
```

### GREEN evidence

Focused orientation, retention, REST, and MCP coverage:

```text
.venv\\Scripts\\python.exe -m pytest -o addopts='--basetemp=.pytest-tmp' -q tests/test_orientation_domain.py tests/test_orientation_repository.py tests/test_orientation_recovery.py tests/test_orientation_correction.py tests/test_retention.py tests/test_rest_api.py tests/test_mcp_api.py
172 passed, 7 skipped in 9.06s
```

Covered boundaries include exact/over-limit capacity, concurrent corrections, mismatched takeover metadata, completion after expiry, repository completion failure after runner acceptance, cleanup-to-restart convergence, public schema stability, and exactly three MCP tools.

Final complete Windows verification:

```text
.venv\\Scripts\\python.exe -m pytest -o addopts='--basetemp=.pytest-tmp' -q
842 passed, 20 skipped in 16.98s

.venv\\Scripts\\python.exe -m pip check
No broken requirements found.

.venv\\Scripts\\python.exe -m compileall -q src tests
git diff --check
# both exited 0
```

## Second final review - expected derivative binding and lock capability

### RED evidence

The durable-binding test failed before implementation because no expected corrected identity was persisted:

```text
.venv\\Scripts\\python.exe -m pytest -q tests/test_orientation_repository.py tests/test_orientation_correction.py -x
FAILED test_restart_rejects_other_filetask_and_forged_digest_without_expected_binding
AttributeError: 'OrientationRecoveryRepository' object has no attribute 'bind_corrected_input'
```

The capability test separately proved the public lease dataclass was forgeable:

```text
.venv\\Scripts\\python.exe -m pytest -q tests/test_orientation_correction.py -k "forged_batch_lease or storage_bound" -x
FAILED test_forged_batch_lease_cannot_publish_derivative
Failed: DID NOT RAISE OrientationFailure
```

### Fix

- Before invoking the runner, the coordinator now atomically binds the corrected input version, UUID, SHA-256, and byte size to the recovery claim. The runner proof adds the adopted source UUID, and both live completion and restart reconciliation compare every durable expected field before accepting the independent result-batch input.
- The expected version uses a new additive column, leaving the older terminal `corrected_input_version` null while CLAIMED. This keeps the additive migration compatible with the previous SQLite state constraint.
- A result-batch FileTask with a different UUID and arbitrary otherwise-valid digest/size cannot complete the claim.
- `BatchLockLease` is now minted only by its owning `FileStorage`. Validation requires the same storage object, current PID, matching batch, active nonce registry entry, and unchanged held lock identity. The registry entry is removed immediately when the context exits, before the OS lock descriptor is released.
- Forged, stale, and cross-storage capabilities fail closed. Real active capabilities still support safe retirement and immutable publication; the batch-size concurrency gate remains protected by the real process-shared OS batch lock.

### GREEN evidence

Focused recovery, retention, and storage coverage:

```text
.venv\\Scripts\\python.exe -m pytest -o addopts='--basetemp=.pytest-tmp' -q tests/test_retention.py tests/test_orientation_domain.py tests/test_orientation_repository.py tests/test_orientation_recovery.py tests/test_orientation_correction.py
146 passed, 7 skipped in 8.28s
```

Final complete verification:

```text
.venv\\Scripts\\python.exe -m pytest -o addopts='--basetemp=.pytest-tmp' -q
845 passed, 20 skipped in 17.90s

.venv\\Scripts\\python.exe -m pip check
No broken requirements found.

.venv\\Scripts\\python.exe -m compileall -q src tests
git diff --check
# both exited 0
```
