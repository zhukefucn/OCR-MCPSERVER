# Task 9B final remediation controller review

## Target and verdict

- Head: `933222eacf61beb41b9fe82c9692f12260d93ca8`
- Remediation range: `319d7d3..933222e`
- Scope: Task 9 marker recovery and metadata lifecycle
- Critical findings: **0**
- Important findings: **1**
- Minor findings: **0**
- Spec verdict: **NOT READY**
- Code-quality verdict: **NOT READY**
- Overall: **NOT READY**

The initialized `0x00` pre-bind recovery path and marker-row purge are correct, and all previous lifecycle closures remain green. One earlier crash boundary remains: the canonical marker is created as an empty file before the creator acquires the OS lock and writes `0x00`. A crash in that interval leaves an empty marker that all future attempts reject, so the required crash-reclaimable lifecycle is not complete.

## Confirmed closures

### Initialized pre-bind marker recovery

An existing unbound marker is adopted only while its descriptor is OS-locked and only when all of the following hold:

- canonical UUID-derived lock name;
- stable data root, `.locks` directory, descriptor identity, and final name binding;
- non-reparse regular file with exactly one link;
- exact size and complete bytes `0x00` before and after the registry transaction;
- no existing `BatchLockMarkerRecord`.

Empty, retired `0x01`, oversized, multi-link, unknown-content, and registered-identity-mismatch markers are rejected without mutation. A post-commit failure retries through the exact durable identity row. The creator now writes `0x00` only while holding the same OS lock, so a concurrent opener either observes the completed marker or releases/retries the empty initialization window.

### Metadata purge

`purge_metadata` deletes `BatchLockMarkerRecord` in the same transaction as artifact, audit, stage-event, file-task, retention, and batch metadata. The exact 30-day boundary and immediate early-delete tests confirm that:

- the DB marker row is gone after commit;
- the on-disk marker remains exactly the content-free retired byte `0x01`;
- a later call cannot adopt, normalize, or mutate that unregistered retired marker.

### Previous lifecycle findings

The prior six closure regressions remain green:

- Windows parent directories remain pinned/exclusively opened through child scrub.
- Multi-link content files are rejected before modification.
- Artifact rollback binds identity, size, link count, and SHA-256 to one open descriptor.
- Packaging holds the shared `FileStorage` batch lock through publish, registration, finalization, rollback, and guard release even after DB-guard expiry.
- Post-register cancellation leaves the available DB row and ZIP consistent.
- A different same-name marker is never adopted or modified on retry.

Canonical batch artifact placement and metadata-phase early-delete resume also remain green.

## Finding

### Important - crash before marker initialization is not recoverable

`_open_lock_file` publishes the canonical file with `O_CREAT|O_EXCL`/`CREATE_NEW` before `batch_lock` acquires its OS lock (`src/ocr_mcp_server/services/file_storage.py:99-103,127-159`). The creator writes `0x00` only after lock acquisition. If the process exits after canonical creation but before acquiring the lock or writing the initialization byte, the marker remains empty with no registry row.

On retry, the existing opener locks the empty file, waits for an initializer up to the bounded retry count, and then rejects it. No creator still exists, so every later retry follows the same path. The batch is permanently wedged even though the empty file was created by the service.

Independent deterministic reproduction:

```text
first attempt: creator crashed before initialization
marker after crash: b''
retry: path_unsafe marker remains: b''
```

This preserves unknown bytes but violates the requirement that a crashed cleaner/writer leave work reclaimable.

Required closure: do not expose an empty canonical marker. Stage and fsync the initialized content-free `0x00` marker under a server-generated sibling name, then atomically publish it without replacement; a crash before publication leaves the canonical name absent and safely retryable. An equivalent durable creation-intent/nonce protocol is acceptable if unknown empty files remain non-adoptable. Add fault injection immediately after canonical creation and before the first lock acquisition/write.

## Verification evidence

Focused lifecycle suite:

```text
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py tests/test_artifacts.py tests/test_file_intake.py tests/test_remote_fetch.py -p no:cacheprovider
132 passed in 6.51s
```

Explicit current and previous closure set:

```text
12 passed in 1.35s
```

Cross-process intake contention:

```text
20/20 consecutive runs passed
```

Fresh repository-wide gate:

```text
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider
669 passed, 5 skipped in 10.63s

.\.venv\Scripts\python.exe -m pip check
No broken requirements found.

.\.venv\Scripts\python.exe -m compileall -q src tests
exit 0, no output

git diff --check 6c3969d..HEAD
exit 0, no output
```

Patch package validation before this report edit:

```text
git apply --check --reverse .superpowers/sdd/review-319d7d3..933222e.diff
exit 0
package Git blob: d8d1d7df6ec236d65b22e3bf26eb7ee6fa1bfeb5
```

## Disposition

Do not advance Task 9B yet. Close the empty canonical-marker crash window, add the focused recovery regression, and repeat the bounded controller review. The requested initialized-marker recovery, purge behavior, prior six lifecycle fixes, and broad verification are otherwise ready.

## Implementer response to the final Important finding

The final recovery protocol handles the canonical empty-file crash state without adopting a general unknown empty object. It combines the already held OS batch lock with a serialized SQLite transaction and a descriptor-bound initializer callback.

### RED - crash after canonical creation and before the first lock

`test_empty_marker_survives_pre_lock_crash_and_is_recovered` injects a process failure from the creator's first `_try_batch_lock` call. At that point the canonical marker has been exclusively created but remains the same regular single-link object with exact contents `b''`; no `BatchLockMarkerRecord` exists.

Before this remediation, a fresh `FileStorage` retry acquired that marker, exhausted the 100-attempt initialization wait, and raised `path_unsafe`. The empty bytes and absent registry row were unchanged, so every future retry followed the same permanent failure.

### GREEN - transactionally authorized handle initialization

`RetentionRepository.bind_empty_lock_marker` now owns this exact sequence:

1. validate the canonical batch ID, non-negative integer identity pair, strict `allow_missing` flag, and synchronous initializer;
2. acquire `BEGIN IMMEDIATE`;
3. require the retention row unless the existing missing-row intake policy permits it;
4. require that no marker registry row exists, rejecting even a same-identity row before invoking the callback;
5. invoke the callback while the caller still holds the OS lock;
6. insert the exact marker identity and commit only after successful callback return.

The callback remains entirely in `FileStorage`. On the same open descriptor it proves the stable data root, `.locks` parent, canonical UUID-derived name, descriptor/name identity, non-reparse regular type, single-link count, and exact zero length. It then writes `0x00`, fsyncs, and repeats the complete proof with exact one-byte contents before the repository can insert or commit.

This covers both crash states:

- a crash before the callback write rolls back the DB transaction and leaves the exact empty marker eligible for the same recovery;
- a crash after `0x00` fsync but before DB commit rolls back the row and leaves the initialized object eligible for the previously verified `0x00` pre-bind recovery.

Any existing registry row, matching or mismatched, rejects before initialization and remains unchanged. Empty and initialized multi-link markers remain untouched. Retired `0x01` and oversized markers never enter the empty initializer. The prior root/parent/name-swap regressions continue to exercise the shared descriptor proof.

`test_live_creator_lock_prevents_a_second_opener_from_initializing` holds an empty creator descriptor under the OS lock, starts a second opener, and proves it remains blocked until the creator writes and fsyncs `0x00` and releases. The existing cross-process capacity regression then passed 20 consecutive runs with the transactional callback protocol.

The former malformed-marker parameter no longer labels `b''` intrinsically malformed: an empty marker is recoverable only through the strict absent-row protocol above. Retired and oversized unknown markers remain in the malformed rejection test, while registered-empty and hard-linked-empty cases have explicit rejection coverage.

## Final empty-marker verification

Focused lifecycle gate, including all prior closure tests:

```text
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py tests/test_artifacts.py tests/test_file_intake.py tests/test_remote_fetch.py -p no:cacheprovider
136 passed in 5.01s
```

Repository-wide gate:

```text
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider
673 passed, 5 skipped in 9.26s

.\.venv\Scripts\python.exe -m pip check
No broken requirements found.

.\.venv\Scripts\python.exe -m compileall -q src tests
exit 0, no output

git diff --check
exit 0 (line-ending notices only)
```

## Final empty-marker disposition

The final Important finding now has a strict RED/GREEN crash regression plus registered-state, link-ownership, live-opener, and cross-process coverage. All prior lifecycle closures remain green. Task 9B is ready for the same controller reviewer to perform the final bounded spec and code-quality review. No push, deployment, or Task 10 work is included.
