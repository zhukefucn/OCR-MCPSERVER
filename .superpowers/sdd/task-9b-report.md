# Task 9B independent controller review

## Review target and verdict

- Range: `47c7b37..595e54b` (`HEAD 595e54bcf8672e996c41b1719042043b1d851bd1`)
- Scope: retention only, including the Task 9A artifact-root integration convention
- Spec verdict: **NOT READY**
- Code-quality verdict: **NOT READY**
- Overall: **NOT READY**

The SQLite phase model is bounded and deterministic, and the ordinary boundary/lease/purge tests pass. The change is nevertheless blocked by two core safety/completeness failures: the POSIX deletion primitive can delete a replacement object after its identity check, and artifact placement is not bound to the batch directory that retention deletes. Early deletion also cannot retry a partial metadata-phase failure.

## Findings

### Critical - POSIX final removal can delete a name-swapped replacement

`OwnedBatchRootDeleter` performs a pathname identity check and then separately calls pathname-based `os.rmdir(staged)` or `os.unlink(staged)` (`src/ocr_mcp_server/services/retention.py:139-164`). Another actor can rename the verified owned object away and place a replacement at the same staged name between those operations. The subsequent call deletes the replacement. This directly violates the binding requirements that cleanup use descriptor/handle-relative deletion or another fail-safe abstraction, and that a name swap never delete the replacement.

The Windows branches bind deletion to an open handle, but the POSIX branches do not. This is a release blocker because Ubuntu is the deployment target, not merely an unverified portability concern.

Adversarial reproduction forced the swap at the final syscall boundary for both branches. Both completed without a retention failure and deleted the attacker replacement:

```text
POSIX regular-file race reproduced: attacker replacement was deleted
POSIX directory race reproduced: attacker replacement was deleted
```

Required closure: keep parent directories open and perform traversal, staging, identity checks, unlink, and rmdir relative to pinned descriptors; do not resolve the staged pathname again for final deletion. Add deterministic adversarial tests for file, nested directory, and final batch-directory swaps on the POSIX branch.

### Critical - the batch-scoped artifact-root convention is neither enforced nor integrated

Retention deletes only `<configured artifact root>/<canonical batch UUID>` (`src/ocr_mcp_server/services/retention.py:242-244,282-284`). Task 9A's public bundler and packaging-step APIs instead accept an arbitrary absolute `artifact_root` and publish `artifact_root/<artifact-id>.zip` (`src/ocr_mcp_server/services/artifacts.py:1238-1273,1495-1504,1656-1677`). Neither API derives nor validates a canonical batch child, and `storage_key` contains only the filename. Existing Task 9A tests commonly pass a flat base directory, so the supposed convention is not represented by the contract.

An integration reproduction placed a ZIP exactly as the current public API/storage key permits and ran due cleanup against that same configured root:

```text
cleanup=1; metadata_available=False; flat_zip_survives=True
```

The service therefore reported content deletion and marked the artifact unavailable while content remained on disk. This violates content-before-marker ordering and the 24-hour/early-deletion guarantees.

Required closure: make one production-owned abstraction derive `<trusted artifact base>/<canonical batch UUID>`; have packaging and retention share it; reject non-canonical batch IDs and any caller-supplied per-batch root that does not exactly match; persist/validate a base-relative storage key that includes the batch segment (or otherwise prove the path binding); and add an end-to-end publish/register/cleanup test using the real bundler.

### Important - early deletion cannot resume after a metadata purge failure

`delete_task` always loops over expected phases `(CONTENT, METADATA)` (`src/ocr_mcp_server/services/retention.py:261-293`). If content cleanup commits but metadata purge fails, retry begins by requiring a content claim. The repository correctly returns a metadata claim, which the service rejects as `cleanup_claim_conflict`. Repeated authorized deletion is therefore not idempotent across the required partial-failure boundary.

Adversarial reproduction:

```text
attempt 1: metadata_purge_failed; phase remains metadata
attempt 2: cleanup_claim_conflict; phase remains metadata
```

Required closure: drive `delete_task` from the repository's current phase, accepting a metadata-only resume, and add fault injection for failure after successful content completion but before metadata purge commit.

### Important - an early-delete request is not a write/publication barrier

Artifact registration rejects only `content_deleted_at is not None`; it explicitly permits registration while `retention.early_delete` is true (`src/ocr_mcp_server/infra/artifact_repository.py:93-99,144-150`). No coordination prevents a pipeline/FileStorage writer from recreating batch content after the deleter has removed a root. A publication racing between filesystem deletion and `complete_content` can be registered and then marked unavailable while its ZIP survives; other in-flight writers can similarly recreate content after the deletion pass.

Required closure: reject artifact registration once early deletion is requested, prevent new pipeline/intake writes for that batch, and coordinate cleanup with the existing batch write lock or an equivalent durable delete barrier. Add deterministic races at the pre-delete, between-roots, and pre-`complete_content` boundaries.

### Minor - per-batch recovery lock files are not included in cleanup

`FileStorage.batch_lock` creates persistent `data_root/.locks/<batch UUID>.lock`, while retention deletes only `data_root/<batch UUID>`. These server-owned per-batch recovery artifacts remain after both ordinary and early deletion. They are content-free, but the brief explicitly includes recovery artifacts in content cleanup. Either remove them safely under the shared lock protocol or document and test a separately bounded purge policy.

## Positive observations

- `claim_due` uses `BEGIN IMMEDIATE`, deterministic due-time/batch-ID ordering, a hard maximum limit, unique tokens, and exact lease-boundary reclaim.
- Artifact unavailable/deleted state is committed only in `complete_content`, after both deleter calls return.
- Metadata purge order is referentially safe for the current schema and occurs in one transaction.
- Stable retention errors discard unsafe causes, and the focused planted-content error-chain test passes.
- No Task 10+ REST/MCP/auth/transport surface or prohibited broker/model dependency was added.

## Verification evidence

Focused repository tests:

```text
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py tests/test_artifacts.py::test_repository_extends_batch_content_retention_to_artifact_expiry -p no:cacheprovider
13 passed in 0.98s
```

Independent adversarial checks:

- POSIX regular-file final-name swap: **failure reproduced; replacement deleted**.
- POSIX empty-directory final-name swap: **failure reproduced; replacement deleted**.
- Flat Task 9A artifact plus due retention cleanup: **failure reproduced; ZIP survived while row became unavailable**.
- Metadata purge fails after committed content phase, then `delete_task` retry: **failure reproduced; retry returned claim conflict**.

Fresh repository-wide controller gate:

```text
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider
642 passed, 5 skipped in 7.75s

.\.venv\Scripts\python.exe -m pip check
No broken requirements found.

.\.venv\Scripts\python.exe -m compileall -q src tests
exit 0, no output

git diff --check 6c3969d..HEAD
exit 0, no output
```

These green broad checks do not exercise the adversarial race/integration failures above and do not change the NOT READY verdict.

## Required disposition

Do not merge or advance Task 9B. Fix both Critical findings and the early-delete retry/barrier findings, add adversarial regressions, then repeat this independent review. The batch-scoped artifact directory must be a checked production invariant, not a caller convention.

## Implementer architecture reassessment before remediation

The controller findings required changing the architecture, not patching individual syscalls:

- A POSIX pathname cannot safely bind a later `unlink`/`rmdir` to an earlier identity check. Cleanup therefore no longer removes descendant names. It atomically isolates the canonical batch directory under a persisted, retry-stable tombstone name, opens directories/files without following links, scrubs regular-file bytes through pinned descriptors/handles, verifies the complete retained tree, and keeps content-free empty tombstones.
- Artifact placement is now owned by the bundler. Its input is the trusted global artifact base plus a canonical lowercase batch UUID; it creates and pins `<base>/<batch UUID>`, publishes only there, and records `<batch UUID>/<artifact-id>.zip` as the storage key.
- Content writers and cleanup now share a durable barrier. Artifact packaging and file intake acquire a retention content-write lease before bytes can become successful. File intake holds that lease inside the existing `FileStorage.batch_lock`. Cleanup first creates its durable DB claim, then takes the same OS batch lock, verifies that prior writers are quiescent, scrubs both owned roots, writes a handle-bound one-byte content-free retired marker, and only then commits `content_deleted_at`. The retired marker prevents an early-deleted UUID from being resurrected after its metadata row is purged.
- Early deletion is repository-state-driven. A retry may resume directly in the metadata phase after content completion, rather than assuming every call starts with content.

This assessment was recorded before the remediation evidence below. The retained empty directory/file tombstones and one-byte lock marker are deliberate content-free safety state; namespace deletion is not used where it would reintroduce a name-swap race.

## Controller remediation RED/GREEN evidence

### Critical 1: name-race-safe content cleanup

- RED: deterministic file and directory swaps showed that the old POSIX check-then-`unlink`/`rmdir` path could delete a replacement.
- GREEN: adversarial tests now cover root replacement before isolation, file replacement after open, opened-directory replacement, persisted-tombstone retry after partial scrub, and a prohibition on descendant `unlink`/`rmdir`. Replacements survive, owned open handles are scrubbed when safe, and DB availability remains unchanged on failure.

### Critical 2: enforced artifact placement

- RED: the bundler accepted flat arbitrary placement, and cleanup could mark an artifact unavailable while that flat ZIP survived.
- GREEN: non-canonical batch IDs are rejected; placement assertions require `<global base>/<canonical batch UUID>/<artifact-id>.zip`; the storage key includes the batch segment; and a real bundler -> repository -> retention-service integration test proves that the published ZIP is scrubbed before the row becomes unavailable.

### Important 1: early metadata resume

- RED: fault injection after committed content cleanup made the next `delete_task` call reject the repository's metadata claim.
- GREEN: `delete_task` follows the claimed current phase. A first metadata purge failure followed by a second authorized call now completes the purge.

### Important 2: reusable write/delete barrier

- RED: artifact publication and input storage could race an early-delete request, allowing filesystem bytes after the deletion pass or an unavailable orphan.
- GREEN: artifact registration requires the exact active content-write guard; packaging scrubs a published archive if registration fails and releases the guard in `finally`. File intake acquires the reusable guard inside the existing batch lock. Deterministic concurrency tests prove both orderings: cleanup-first blocks intake before bytes land, while intake-first makes cleanup wait and then remove the completed write. Missing-row canonical new batches remain writable; early-deleted UUIDs are blocked by the retired marker even after metadata purge. A lock-name swap preserves the replacement and prevents the DB content marker from advancing.

### Minor: recovery lock artifact

- RED: `.locks/<batch UUID>.lock` survived without a defined post-retention meaning.
- GREEN: the held lock descriptor is normalized to server-owned content-free bytes and retired as the single byte `0x01` before `content_deleted_at` commits. The exact open handle and final name identity are verified. This bounded marker is intentionally retained to prevent post-purge UUID resurrection.

### Verification after remediation

- Focused artifact, retention, file-intake, and remote-fetch suites pass.
- `\.venv\Scripts\python.exe -m pytest -p no:cacheprovider` -> `655 passed, 5 skipped in 8.00s`.
- `\.venv\Scripts\python.exe -m pip check` -> `No broken requirements found.`
- `\.venv\Scripts\python.exe -m compileall -q src tests` -> exit 0 with no output.
- `git diff --check` -> exit 0; Git emitted line-ending conversion notices only.
