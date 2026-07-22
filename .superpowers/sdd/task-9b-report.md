# Task 9B second-remediation controller review

## Target and verdict

- Head: `319d7d3b27e8e8d9d8616b924c8476f896250723`
- Remediation range: `f74ab0f..319d7d3`
- Scope: Task 9 lifecycle consistency only
- Critical findings: **0**
- Important findings: **2**
- Minor findings: **0**
- Spec verdict: **NOT READY**
- Code-quality verdict: **NOT READY**
- Overall: **NOT READY**

All six findings from the previous review are closed in the targeted implementation and regression tests. Canonical batch artifact placement and metadata-phase early-delete resume also remain correct. The new durable lock-marker registry introduces one unrecoverable filesystem/DB partial-failure boundary and retains per-task metadata beyond the required 30-day purge, so the lifecycle is not yet ready.

## Prior-finding closure

| Finding | Result | Controller evidence |
|---|---|---|
| Windows parent replacement during scrub | **Closed** | The configured root and isolated tombstone are pinned; Windows tombstone/nested-directory handles deny delete sharing and final paths are checked before and after mutation. The two Windows regressions pass. |
| Multi-link file modification | **Closed** | `_scrub_open_regular` requires `st_nlink == 1` before truncation and verifies it afterward. The hard-link integration regression preserves external bytes and leaves DB availability unchanged. |
| Artifact rollback object binding | **Closed** | The bundle carries the publication identity; rollback opens once and verifies identity, size, link count, and full SHA-256 before scrubbing that descriptor. Same-name replacement is preserved. |
| Expired artifact guard bypass | **Closed** | `ArtifactPackagingStep` holds `FileStorage.batch_lock` around guard acquisition, publish, register, rollback, guard release, and finalization. Cleanup remains blocked after the DB guard expires until packaging releases the OS lock. |
| Post-register cancellation inconsistency | **Closed** | Registration is tracked as the commit point; exceptions after commit do not scrub the ZIP. The available row's size/hash remain consistent with disk. |
| Lock-name replacement on retry | **Closed** | Existing marker identity must match `batch_lock_markers` before marker bytes are read or normalized. The two-attempt name-swap regression preserves the replacement unchanged. |

Previously closed behavior also remains covered:

- The bundler requires a canonical lowercase batch UUID, publishes under `<artifact base>/<batch UUID>`, and stores `<batch UUID>/<artifact-id>.zip`.
- A second early-delete call resumes directly in metadata after an injected first metadata-purge failure.
- Tombstone names remain persisted before isolation, and artifact availability advances only after both content roots and the retired marker complete successfully.

## Findings

### Important - first marker-bind failure permanently wedges the batch

`FileStorage.batch_lock` exclusively creates the marker file and writes `0x00` before calling `marker_registry.bind_lock_marker` (`src/ocr_mcp_server/services/file_storage.py:99-155`). `RetentionRepository.bind_lock_marker` refuses to create a registry row for an already-existing marker because the next attempt reports `created=False` (`src/ocr_mcp_server/infra/retention_repository.py:67-104`).

If SQLite binding fails or the process crashes after filesystem creation but before the first registry commit, the filesystem marker survives without a registry row. Every retry then classifies that service-created marker as an unknown object and fails ownership validation. This is fail-safe with respect to replacement bytes, but it is not retryable and a crashed cleaner/writer cannot recover.

Independent deterministic reproduction:

```text
first attempt: cleanup_claim_conflict
orphan marker exists: True bytes: b'\x00'
retry: cleanup_ownership_invalid
```

Required closure: persist a content-free creation intent/nonce before publishing the marker, then make retry able to reconcile only a marker carrying that exact intent. An unknown same-name marker must still remain untouched. Add fault injection immediately before and immediately after the first registry commit.

### Important - marker registry rows bypass the 30-day metadata purge

`BatchLockMarkerRecord` is keyed by batch ID but has no foreign key or expiry, and `purge_metadata` never deletes it (`src/ocr_mcp_server/infra/task_models.py:167-171`; `src/ocr_mcp_server/infra/retention_repository.py:400-415`). Every processed task therefore leaves a permanent SQLite row after artifact/audit/task metadata is purged at 30 days.

The stored device/inode identity is content-free, but it is still per-task lifecycle metadata and grows without a bounded purge, contrary to the configured 30-day content-free metadata policy. The on-disk retired marker can continue preventing UUID resurrection without retaining this DB row: once the registry row is purged, the existing name is unknown and is already rejected without mutation.

Required closure: delete the marker registry row in the referentially ordered metadata purge, or give it an explicit bounded expiry consistent with the 30-day policy. Add a boundary test showing no `batch_lock_markers` row remains after ordinary and immediate early metadata purge while the one-byte retired marker remains content-free and non-adoptable.

## Verification evidence

Focused lifecycle suite:

```text
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py tests/test_artifacts.py tests/test_file_intake.py tests/test_remote_fetch.py -p no:cacheprovider
126 passed in 4.66s
```

Explicit closure set (six prior findings plus canonical placement and metadata resume):

```text
9 passed in 1.09s
```

Fresh repository-wide gate:

```text
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider
663 passed, 5 skipped in 8.31s

.\.venv\Scripts\python.exe -m pip check
No broken requirements found.

.\.venv\Scripts\python.exe -m compileall -q src tests
exit 0, no output

git diff --check 6c3969d..HEAD
exit 0, no output
```

Patch package validation before this report edit:

```text
git apply --check --reverse .superpowers/sdd/review-f74ab0f..319d7d3.diff
exit 0
package Git blob: 50e753bf03dbeb1a108f3a7679b13feccec1494c
```

## Disposition

Do not advance Task 9B yet. Close the first-bind crash/retry protocol and include the marker registry in bounded metadata retention, then repeat the focused controller review. No Critical safety regression remains in the six previously reported paths.

## Implementer response to the two remaining findings

The two findings were addressed under one constrained marker invariant: only the exact fresh one-byte marker `0x00` can recover a missing first registry binding, while the retired marker remains the distinct byte `0x01` and is never adoptable after its registry metadata is purged.

### Important - pre-bind crash recovery

RED: `test_fresh_marker_survives_pre_bind_crash_and_is_recovered` injected failure after the canonical marker had been created and durably initialized but before the first registry call could commit. The marker remained `b"\x00"`, the registry row remained absent, and retry failed with `cleanup_ownership_invalid` because it reached `marker is None` with `created=False`.

GREEN: `FileStorage.batch_lock` now performs marker decisions only while holding the OS lock on the same open descriptor. It requires a canonical UUID-derived name, stable data-root/`.locks`/name binding, non-reparse regular type, link count one, stable identity, exact size, and exact full marker bytes before passing `recover_unbound=True`. `RetentionRepository.bind_lock_marker` accepts that authorization only when its `BEGIN IMMEDIATE` transaction still finds no row. An existing row always requires exact identity equality and is never updated or bypassed by recovery authorization.

The recovery set covers both crash boundaries and rejection cases:

- pre-bind failure retries and registers the original marker identity;
- post-commit failure retries through the already durable exact-identity row;
- empty, retired `0x01`, oversized, and hard-linked unbound markers are rejected without mutation or registry insertion;
- the prior name-swap retry now uses an exact fresh-byte `0x00` replacement and proves that an existing registry mismatch is still rejected without mutation.

During the required fresh baseline, the existing cross-process intake regression exposed the marker initialization window independently: one run returned `path_unsafe` instead of the capacity error. After moving validation behind the OS lock, stress reproduction identified both sides of the remaining race in the same run:

```text
existing opener: OSError('invalid batch lock marker')
creator: PermissionError(13, 'Permission denied')
result: [('error', 'path_unsafe'), ('error', 'path_unsafe')]
```

The existing opener had locked the newly created but still-empty file while the creator attempted to write its initialization byte through that byte-range lock. Creation now acquires the OS lock before writing `0x00`. An existing opener that locks an empty marker releases, yields, and reacquires for a bounded initialization window; it never adopts or mutates the empty object. The cross-process regression then passed 20 consecutive runs, and the final focused/full gates also passed it.

### Important - bounded marker metadata retention

RED: the extended exact-30-day boundary test and immediate early-delete test each observed one remaining `BatchLockMarkerRecord` after every other per-task metadata row had been purged.

GREEN: `purge_metadata` now deletes the marker registry row inside the existing transaction, after dependent audit/artifact/event/file rows and before retention/batch rows. Both ordinary exactly-due and immediate early deletion leave no marker registry row. The on-disk marker remains the content-free retired byte `0x01`; a direct later `batch_lock(..., allow_retired=True)` with no registry row rejects it as `cleanup_ownership_invalid` and preserves the byte unchanged. The injected metadata-purge failure still resumes successfully.

## Final remediation verification

Focused lifecycle gate including all six prior closures, the two remaining findings, and cross-process intake:

```text
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py tests/test_artifacts.py tests/test_file_intake.py tests/test_remote_fetch.py -p no:cacheprovider
132 passed in 6.30s
```

Repository-wide gate:

```text
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider
669 passed, 5 skipped in 10.49s

.\.venv\Scripts\python.exe -m pip check
No broken requirements found.

.\.venv\Scripts\python.exe -m compileall -q src tests
exit 0, no output

git diff --check
exit 0 (line-ending notices only)
```

## Final remediation disposition

Both remaining Important findings now have strict RED/GREEN coverage, and all six previous closures remain green. Task 9B is ready for the same controller reviewer to repeat spec and code-quality review. No push, deployment, or Task 10 work is included.
