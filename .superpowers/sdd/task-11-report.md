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
