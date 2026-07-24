# Task 9B Marker Recovery and Purge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover only an exact fresh unbound batch-lock marker after a pre-bind crash and purge the per-task marker registry row at the existing metadata deadline.

**Architecture:** `FileStorage.batch_lock` proves recovery eligibility from one locked descriptor and stable root/parent/name bindings, then passes an explicit `recover_unbound` authorization to a serialized repository transaction. `RetentionRepository.purge_metadata` deletes the independent marker row in the same transaction as the remaining task metadata, leaving the distinct retired on-disk byte non-adoptable.

**Tech Stack:** Python 3.11, asyncio, SQLAlchemy async SQLite, descriptor-relative POSIX I/O, Windows handle APIs, pytest/pytest-asyncio.

## Global Constraints

- Use strict RED/GREEN TDD for each finding.
- Adopt only canonical `<lowercase UUID>.lock`, exact `b"\x00"`, non-reparse regular single-link markers with stable descriptor/name/parent identity.
- Never adopt or rewrite an existing registry mismatch, malformed marker, replacement marker, or retired `b"\x01"` marker.
- Preserve all six previous remediation closures, canonical artifact placement, and metadata-phase early-delete resume.
- No push, deployment, Task 10, external-service, network, or GPU work.

---

### Task 1: Recover a proven fresh unbound marker

**Files:**
- Modify: `tests/test_retention.py`
- Modify: `src/ocr_mcp_server/services/file_storage.py`
- Modify: `src/ocr_mcp_server/infra/retention_repository.py`
- Modify: `tests/test_file_intake.py`
- Modify: `tests/test_remote_fetch.py`
- Modify: `tests/test_artifacts.py`

**Interfaces:**
- Consumes: `FileStorage.batch_lock(batch_id, marker_registry=..., allow_missing_marker=..., allow_retired=...)`.
- Produces: `RetentionRepository.bind_lock_marker(batch_id, identity, *, created, recover_unbound, allow_missing)`.
- Invariant: `recover_unbound=True` is evidence only; a repository row that appears or already exists still requires exact identity equality.

- [ ] **Step 1: Write the pre-bind crash regression**

Add a test that wraps `repository.bind_lock_marker`, raises `RetentionFailure(CLAIM_CONFLICT)` on its first call after asserting the marker is exactly `b"\x00"`, then restores normal binding and retries the same `FileStorage.batch_lock`. Assert the first call leaves no `BatchLockMarkerRecord`, the retry succeeds, and the inserted identity equals the still-open marker's identity.

- [ ] **Step 2: Run the crash regression and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py::test_fresh_marker_survives_pre_bind_crash_and_is_recovered -q -p no:cacheprovider
```

Expected: FAIL on the retry with `cleanup_ownership_invalid`, because `created=False` cannot insert the missing row.

- [ ] **Step 3: Write malformed, replacement, and post-commit regressions**

Add focused tests that assert:

```python
assert malformed_path.read_bytes() == malformed
assert replacement_path.read_bytes() == replacement
assert registered.identity == encoded_original_identity
```

The malformed cases include empty, oversized, retired `b"\x01"`, and hard-linked fresh-byte files. The replacement case first registers an original identity, replaces the name with an exact fresh one-byte file, and proves the mismatch is rejected without mutation. The post-commit injection raises after delegating to the real bind method and proves retry uses the durable exact-identity row.

- [ ] **Step 4: Run new rejection tests and verify their expected RED/GREEN baseline**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py -k 'fresh_marker or malformed_unbound or post_bind or lock_name_replacement' -q -p no:cacheprovider
```

Expected before implementation: the crash-retry recovery test fails; existing malformed/replacement safety cases remain unchanged or expose missing single-link/exact-size validation.

- [ ] **Step 5: Implement descriptor-bound recovery proof**

In `FileStorage.batch_lock`, move every existing-marker size/content decision after acquiring the OS lock, then prove recovery eligibility before repository binding:

```python
info = os.fstat(descriptor)
recover_unbound = (
    not created
    and stat.S_ISREG(info.st_mode)
    and not self._is_reparse(info)
    and info.st_nlink == 1
    and info.st_size == 1
    and self._read_exact_marker(descriptor) == b"\x00"
    and lock_unchanged()
)
await marker_registry.bind_lock_marker(
    canonical_batch_id,
    lock_identity,
    created=created,
    recover_unbound=recover_unbound,
    allow_missing=allow_missing_marker,
)
```

Revalidate the descriptor type, link count, identity, exact size/content, root/parent/name identity after binding before any normalization or yield. Update simple test registries to accept the new keyword.

- [ ] **Step 6: Implement atomic absent-row adoption**

Validate `recover_unbound` as a strict boolean. Within the existing `BEGIN IMMEDIATE` transaction:

```python
if marker is None:
    if not (created or recover_unbound):
        raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
    session.add(BatchLockMarkerRecord(batch_id=batch_id, identity=encoded))
elif marker.identity != encoded:
    raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
```

Do not update an existing row and do not use `recover_unbound` to bypass an identity mismatch.

- [ ] **Step 7: Verify GREEN and all prior marker/barrier regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py tests/test_file_intake.py tests/test_remote_fetch.py tests/test_artifacts.py -k 'marker or lock or barrier or replacement or post_register or hardlink or windows_parent' -q -p no:cacheprovider
```

Expected: all selected tests pass.

---

### Task 2: Purge the marker registry row at metadata expiry

**Files:**
- Modify: `tests/test_retention.py`
- Modify: `src/ocr_mcp_server/infra/retention_repository.py`

**Interfaces:**
- Consumes: the existing metadata-phase `RetentionClaim` and `purge_metadata(claim, now=...)` transaction.
- Produces: no `BatchLockMarkerRecord` for the purged batch while leaving the on-disk retired marker untouched.

- [ ] **Step 1: Extend the exact 30-day boundary regression and verify RED**

Import `BatchLockMarkerRecord`, assert one marker row after content cleanup and at `metadata_due_at - 1 microsecond`, then assert zero after the exactly-due purge. Also assert the on-disk lock path remains exactly `b"\x01"`.

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py::test_metadata_remains_until_exact_30_day_boundary_then_purges_all_task_rows -q -p no:cacheprovider
```

Expected: FAIL because the marker row count remains one after metadata purge.

- [ ] **Step 2: Extend early deletion coverage and verify RED**

Assert that immediate `delete_task` leaves no `BatchLockMarkerRecord`, preserves the on-disk `b"\x01"`, and that a later direct `batch_lock(..., allow_retired=True)` cannot adopt it without registry metadata.

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py::test_early_deletion_runs_both_phases_and_is_idempotent -q -p no:cacheprovider
```

Expected: FAIL because the marker row remains after immediate purge.

- [ ] **Step 3: Delete marker metadata in the purge transaction**

Insert this deletion after task dependents and before retention/batch deletion:

```python
await session.execute(
    delete(BatchLockMarkerRecord).where(
        BatchLockMarkerRecord.batch_id == claim.batch_id
    )
)
```

- [ ] **Step 4: Verify GREEN for boundary, early deletion, and resume behavior**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py -k 'metadata_remains_until_exact or early_deletion or early_deletion_resumes' -q -p no:cacheprovider
```

Expected: all selected tests pass.

---

### Task 3: Report and repository gates

**Files:**
- Append: `.superpowers/sdd/task-9b-report.md`

**Interfaces:**
- Consumes: recorded RED and GREEN command output from Tasks 1 and 2.
- Produces: controller-readable evidence without replacing the current NOT READY review.

- [ ] **Step 1: Run the focused lifecycle gate**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py tests/test_artifacts.py tests/test_file_intake.py tests/test_remote_fetch.py -p no:cacheprovider
```

- [ ] **Step 2: Run the full repository gate**

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q src tests
git diff --check
```

- [ ] **Step 3: Append exact evidence and commit**

Append the recovery invariant, each RED symptom, each GREEN regression, focused/full counts, and the no-push/no-deploy disposition. Commit implementation/tests separately from the report/plan update.

- [ ] **Step 4: Request same-reviewer re-review**

Send the local commit hashes and verification results to the controller and explicitly request the same reviewer repeat spec and code-quality review.

---

### Task 4: Recover a canonical marker left empty before creator locking

**Files:**
- Modify: `tests/test_retention.py`
- Modify: `tests/test_file_intake.py`
- Modify: `tests/test_remote_fetch.py`
- Modify: `tests/test_artifacts.py`
- Modify: `src/ocr_mcp_server/infra/retention_repository.py`
- Modify: `src/ocr_mcp_server/services/file_storage.py`
- Append: `.superpowers/sdd/task-9b-report.md`

**Interfaces:**
- Produces: `RetentionRepository.bind_empty_lock_marker(batch_id, identity, *, allow_missing, initialize: Callable[[], None]) -> None`.
- Consumes: the OS-locked descriptor and existing `read_locked_marker()` proof in `FileStorage.batch_lock`.
- Invariant: the callback executes inside `BEGIN IMMEDIATE` only when retention policy permits the batch and no registry row exists; only after callback fsync/revalidation does the transaction insert and commit the identity.

- [ ] **Step 1: Write the empty-creation crash regression**

Inject a crash from the creator's first `_try_batch_lock` call, after `_open_lock_file` has published the zero-length canonical marker. Assert the marker is `b""` and no registry row exists, then retry with a fresh `FileStorage` and require the same filesystem identity to be initialized to `b"\x00"` and registered.

- [ ] **Step 2: Verify the crash regression is RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py::test_empty_marker_survives_pre_lock_crash_and_is_recovered -q -p no:cacheprovider
```

Expected: FAIL after the bounded initialization wait with `path_unsafe`, leaving the marker empty and unregistered.

- [ ] **Step 3: Add safety and live-opener regressions**

Add tests proving that an empty marker with either matching or mismatched registry identity is rejected before any callback write, an empty multi-link marker is preserved, and a second live opener blocks on a creator-held OS lock until that creator writes/fsyncs `0x00` and releases it.

- [ ] **Step 4: Implement transactional empty binding**

Add the repository method with this transaction order:

```python
await session.execute(text("BEGIN IMMEDIATE"))
retention = await session.get(RetentionRecord, batch_id)
if retention is None and not allow_missing:
    raise RetentionFailure(RetentionErrorCode.CLAIM_CONFLICT)
if await session.get(BatchLockMarkerRecord, batch_id) is not None:
    raise RetentionFailure(RetentionErrorCode.CLEANUP_OWNERSHIP)
initialize()
session.add(BatchLockMarkerRecord(batch_id=batch_id, identity=encoded))
await session.commit()
```

Validate canonical batch ID, non-negative integer identity pair, strict boolean `allow_missing`, and callable initializer before opening the transaction. Propagate initializer filesystem failures so the session rolls back without inserting.

- [ ] **Step 5: Initialize only through the locked descriptor**

Replace the bounded empty-marker rejection loop with one repository callback. The callback must call `read_locked_marker()` and require `b""`, write `b"\x00"` through the same descriptor, fsync it, then require `read_locked_marker() == b"\x00"`. After the repository returns, call the normal bind path again; the exact committed identity must match. A creator whose empty marker was initialized by a competing opener while it waited must accept only the resulting exact `b"\x00"`/identity binding.

- [ ] **Step 6: Update in-memory marker registries**

Each test-only registry implements:

```python
async def bind_empty_lock_marker(self, *_args, initialize, **_kwargs):
    initialize()
```

This preserves real descriptor behavior in file-intake, remote-fetch, and artifact tests without pretending to provide SQLite durability.

- [ ] **Step 7: Verify GREEN and all lifecycle gates**

Run the new crash/safety/live-opener tests, the focused four-file lifecycle gate, the full repository suite, `pip check`, `compileall`, and `git diff --check`. Append exact RED/GREEN evidence to the controller report, commit implementation/tests separately from documentation, and request the same reviewer inspect the new HEAD.
