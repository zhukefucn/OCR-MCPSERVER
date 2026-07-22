# Task 9B remediation controller re-review

## Target and verdict

- Head: `f74ab0fecab3c1b60b75f8664d1bcf2f84fb03d1`
- Remediation range: `595e54b..f74ab0f`
- Scope: Task 9 lifecycle cleanup only
- Spec verdict: **NOT READY**
- Code-quality verdict: **NOT READY**
- Overall: **NOT READY**

The remediation closes canonical artifact placement and early metadata-phase resume, and it substantially improves partial-crash recovery by persisting tombstone names before isolation. It is still blocked by ownership races in handle scrubbing and by an artifact writer barrier that is lease-only rather than coordinated with cleanup's shared OS batch lock.

## Prior-finding closure

| Prior finding | Result | Evidence |
|---|---|---|
| C1: name-swap-safe deletion | **Not closed** | Descendant `unlink`/`rmdir` was removed and POSIX traversal is descriptor-relative, but Windows child traversal can scrub a replacement after a parent swap. The scrubber also truncates multi-link files without proving that their bytes are confined to the batch. |
| C2: batch-scoped artifact root | **Closed** | The bundler requires a canonical lowercase batch UUID, derives `<artifact base>/<batch UUID>`, and persists `<batch UUID>/<artifact-id>.zip`; the real publish/register/cleanup test passes. |
| I1: metadata-phase early-delete resume | **Closed** | `delete_task` follows the repository's claimed current phase; injected first-purge failure resumes successfully on the second call. |
| I2: durable write/delete barrier | **Not closed** | File intake holds the OS batch lock, but artifact packaging only holds a fixed 300-second DB lease with no renewal or OS lock. Cleanup treats an expired guard as quiescent even while its holder is still running. |
| Minor: retired lock marker | **Not closed** | The marker is content-free (`0x01`) and the first swap is detected, but retry opens and truncates an unknown same-name replacement before ownership can be re-established. |

## Findings

### Critical - tombstone scrubbing is not consistently bound to the owned object

On Windows, `_scrub_directory` verifies the tombstone parent, then separately resolves `child_path` with `os.lstat` and opens it by pathname (`src/ocr_mcp_server/services/retention.py:98-137,144-175`). A parent-directory swap between the parent check and child lookup makes both the lookup and open refer to the replacement child. Cleanup truncates that replacement and only notices the parent mismatch during final verification.

Independent deterministic reproduction:

```text
cleanup failed closed with: cleanup_ownership_invalid
owned bytes: b'owned-original'
replacement bytes: b''
WINDOWS parent-swap race reproduced: attacker replacement was scrubbed
```

The scrubber also accepts every regular file without checking link ownership. A hard link inside the batch to a regular file outside the batch is truncated through the open handle (`src/ocr_mcp_server/services/retention.py:131-175`):

```text
link count before: 2
external bytes after cleanup: b''
HARDLINK confinement bug reproduced: cleanup scrubbed external bytes
```

The DB content marker does not advance in the Windows swap case, so retry state is preserved, but "fail closed" is insufficient after replacement bytes have already been destroyed. Required closure is handle-relative Windows traversal rooted at the pinned parent handle, plus rejection of multi-link regular files unless ownership can be proven. Add regressions for the exact parent-swap boundary and hard-link confinement.

### Critical - artifact reconciliation can scrub a same-name replacement

`ArtifactBundler.scrub_published` derives ownership from the current `bundle.path`, stats the current name, opens that current object, and truncates it (`src/ocr_mcp_server/services/artifacts.py:1681-1724`). `ArtifactBundle` carries no publication identity or still-open handle that binds reconciliation to the object originally published. A swap between publish and reconciliation therefore destroys the replacement:

```text
owned archive size: 4364
replacement bytes: b''
ARTIFACT reconciliation race reproduced: replacement ZIP was scrubbed
```

Required closure: retain a publication identity/handle through registration and scrub only that exact object. If exact identity cannot be proven, leak and retry; never truncate the current same-name object merely because it is a regular file.

### Critical - artifact writes do not share cleanup's durable OS barrier

`ArtifactPackagingStep` acquires a hard-coded 300-second DB guard and then packages/registers without taking `FileStorage.batch_lock` or renewing the guard (`src/ocr_mcp_server/services/artifacts.py:1758-1821`). `require_content_write_quiescent` clears an expired token and permits cleanup (`src/ocr_mcp_server/infra/retention_repository.py:322-340`). Unlike file intake, a live artifact writer that exceeds the fixed lease is not blocked by the OS lock while cleanup isolates and scrubs the artifact directory.

Independent repository reproduction retained the live guard object while cleanup accepted quiescence at its exact expiry:

```text
cleanup accepted quiescence while writer still holds guard: <live token>
```

This can let cleanup reach the retired marker/DB transition while a slow 1 GiB publication still holds the old directory and can continue writing. Required closure: artifact packaging must participate in the same batch OS lock for the full write/reconcile interval, or implement a renewable lease with a proven stop-writing protocol before expiry. Add deterministic overrun and crash-at-expiry tests; no real sleep is needed.

### Important - post-registration failures create an available row for a scrubbed archive

The packaging step catches every exception after `bundle` exists and scrubs it, including cancellation or progress failure after `repository.register` has committed (`src/ocr_mcp_server/services/artifacts.py:1808-1821`). The metadata row remains available with the original size/hash while the archive is zero bytes.

Independent reproduction injected cancellation at the post-register checkpoint:

```text
pipeline raised: post-register cancellation
row_available= True row_size= 4364 disk_size= 0
POST-REGISTER SCRUB bug reproduced: available row points to scrubbed archive
```

Required closure: track whether registration committed. Scrub only before a successful DB commit; after commit, either return the committed result despite nonessential progress failure or transactionally make the row unavailable before scrubbing the exact published object.

### Important - a retry mutates the same-name lock replacement

`batch_lock` treats any non-retired regular file at the canonical lock name as an active service marker and writes/truncates it to one byte before yielding (`src/ocr_mcp_server/services/file_storage.py:92-141`). After the tested first-attempt name swap, a retry therefore opens the replacement and changes its bytes before it can prove continuity with the original lock.

Independent reproduction after replacing a previously initialized lock name:

```text
before retry b'attacker-replacement'
retry failed: path_unsafe bytes now: b'\x00'
after retry b'\x00'
```

The retained `0x01` marker itself is content-free, but the name-swap recovery is not safe. Persist sufficient marker identity/state before mutation, reject unknown existing lock objects, and test the second cleanup attempt after the first swap failure.

## Confirmed behavior

- Content and metadata due boundaries remain exactly 24 hours and 30 days with configurable positive settings.
- SQLite cleanup claims remain bounded, deterministic, exclusive, and reclaimable at the lease boundary.
- Tombstone names are persisted before directory isolation; partial scrub retries use the same tombstone.
- The ordinary and injected partial-failure paths update artifact availability only after both owned roots are reported scrubbed and the retired marker is written.
- Metadata purge remains referentially ordered and transactional.
- Canonical batch artifact placement and storage-key validation are integrated with the real bundler.
- Early deletion resumes directly in metadata after a first purge failure.
- File intake and cleanup serialize on the same OS batch lock in both tested orderings.
- Stable errors and retained SQLite metadata remain content-free in the reviewed paths.
- No Task 10 endpoint/auth/MCP work was introduced.

## Verification

Focused remediation suites:

```text
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py tests/test_artifacts.py tests/test_file_intake.py tests/test_remote_fetch.py -p no:cacheprovider
118 passed in 4.10s
```

Fresh repository-wide gate:

```text
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider
655 passed, 5 skipped in 8.42s

.\.venv\Scripts\python.exe -m pip check
No broken requirements found.

.\.venv\Scripts\python.exe -m compileall -q src tests
exit 0, no output

git diff --check 6c3969d..HEAD
exit 0, no output
```

Patch package validation before this report edit:

```text
git apply --check --reverse .superpowers/sdd/review-595e54b..f74ab0f.diff
exit 0
package Git blob: a25cf29fc460fd705702124ff784bef2c21582e7
```

The broad test suite is green, but it does not cover the independently reproduced ownership and post-registration lifecycle failures.

## Disposition

Do not advance Task 9B. Close the three Critical ownership/barrier findings and both Important consistency findings, add focused regressions, and repeat controller review.

## Implementer architecture assessment before second remediation

The six findings share one root cause: ownership was checked independently at adjacent layers, but no single invariant remained valid from writer admission through filesystem mutation and DB publication.

The second remediation therefore uses one ownership/locking/binding invariant:

1. Every batch writer and cleanup operation enters the same canonical `FileStorage` batch-lock domain before it can publish, register, isolate, scrub, or advance retention metadata.
2. The durable retention row binds that lock name to the exact server-created marker identity. An existing same-name object whose identity is unknown or differs is never normalized, truncated, removed, or adopted during retry.
3. Every directory chain used for scrubbing remains pinned for the complete child operation. On Windows, the pinned parent denies delete/rename sharing; children are opened while that parent is stable, and handle identity/final path plus parent identity are checked before and after mutation. If the chain cannot be proven stable, cleanup fails before touching the candidate.
4. A regular file is scrub-eligible only when its open handle identifies the expected object and its link count is exactly one. Multi-link files are outside the owned-byte boundary and are rejected untouched.
5. An artifact rollback is bound to the publication identity, expected size, and digest returned by the publish operation. Reconciliation opens once without following links, verifies all three properties on that descriptor, scrubs that same descriptor, then verifies that the name still binds to it. A same-name replacement is preserved.
6. DB registration is the artifact commit point. Before commit, rollback may scrub the exact published object; after commit, cancellation or progress failure never scrubs an available artifact.

This assessment was written before production changes or second-remediation GREEN results. Canonical batch artifact placement and state-driven metadata resume remain unchanged.

## Second remediation implementation and RED/GREEN evidence

The implementation now applies the invariant above at every destructive or publishing boundary.

### C1 - Windows parent replacement

RED: the deterministic Windows seam could not request an exclusive directory handle, and the existing pathname-based child traversal allowed a replacement parent to supply the object that was scrubbed. The reproduction left the owned bytes intact but reduced the replacement to `b''`.

GREEN: cleanup pins the configured root, reopens the isolated tombstone with delete/rename sharing denied, and verifies the final path and object identity of the directory and child handles before and after mutation. `test_windows_exclusive_parent_handle_blocks_directory_replacement` and `test_windows_parent_replacement_is_blocked_or_preserved_before_child_scrub` now prove that the replacement is either blocked or preserved untouched. The pre-existing child-rename regression was strengthened to require the moved original's bytes to remain unchanged when final-path binding is lost.

### C2 - hard-linked descendant confinement

RED: `test_hardlinked_descendant_is_rejected_without_touching_external_bytes` reproduced a batch descendant with link count two and observed the external file truncated by cleanup.

GREEN: the scrubber now requires `st_nlink == 1` before mutation and again after descriptor scrubbing. The same integration test now reports cleanup failure, preserves the external bytes, and keeps the artifact metadata available for retry.

### C3 - exact artifact rollback binding

RED: `test_artifact_rollback_preserves_a_same_name_replacement` replaced the published ZIP before reconciliation; the prior rollback truncated the replacement and returned without an ownership error.

GREEN: `ArtifactBundle` carries the publication `(st_dev, st_ino)` identity. Reconciliation opens the current name once without following links, then requires that identity, the registered byte count, a single link, the full SHA-256 digest, and stable descriptor/name binding before scrubbing that same descriptor. A same-name replacement is rejected and preserved.

### C4 - shared cleanup/writer barrier

RED: the new deterministic overrun test initially failed because `ArtifactPackagingStep` had no batch-lock dependency; packaging held only the expiring DB lease.

GREEN: `ArtifactPackagingStep` now requires `FileStorage` and a marker registry and holds the same OS batch lock used by cleanup around DB-guard acquisition, publication, registration, rollback, and guard release. `test_expired_artifact_guard_cannot_bypass_the_shared_batch_lock` pauses publication until the DB lease has expired, starts cleanup, and proves cleanup remains blocked on the OS lock until packaging has stopped writing.

### I1 - registration as the artifact commit point

RED: `test_post_register_cancellation_never_scrubs_an_available_artifact` injected cancellation immediately after registration and observed an available row of 4,364 bytes pointing to a zero-byte archive.

GREEN: the step records successful registration and only performs rollback before that commit point. The same test now observes the propagated cancellation while the available row's size and digest still match the archive on disk.

### I2 - durable lock-marker ownership

RED: `test_cleanup_retry_never_adopts_or_mutates_a_lock_name_replacement` reproduced a failed cleanup followed by replacement of the marker name; the prior retry succeeded and rewrote the replacement. `test_cleanup_rejects_an_unknown_preexisting_lock_without_mutation` also showed that a pre-existing unknown marker was adopted.

GREEN: the new `batch_lock_markers` registry persists the exact server-created lock-object identity independently of the retention row, including after metadata purge. Lock acquisition creates a new marker with exclusive-create semantics or validates an existing marker against the registry while holding its OS lock. Unknown and mismatched marker objects are rejected before any write, truncate, or normalization. Both regressions now preserve the replacement bytes and fail closed.

## Second remediation verification

Focused lifecycle suites after all implementation changes:

```text
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py tests/test_artifacts.py tests/test_file_intake.py tests/test_remote_fetch.py -p no:cacheprovider
126 passed in 4.53s
```

Repository-wide gate after the final ownership check:

```text
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider
663 passed, 5 skipped in 9.10s

.\.venv\Scripts\python.exe -m pip check
No broken requirements found.

.\.venv\Scripts\python.exe -m compileall -q src tests
exit 0, no output

git diff --check
exit 0 (line-ending notices only)
```

## Second remediation disposition

All six re-review findings have implementation coverage and deterministic regression coverage. Task 9B is ready for the same controller reviewer to repeat the spec and code-quality review. No push, deployment, or Task 10 work is included.
