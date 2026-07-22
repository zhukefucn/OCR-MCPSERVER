# Task 9B final controller review

## Target and verdict

- Head: `69021de9c09754903639be9f76fbe8b559a896b4`
- Remediation range: `933222e..69021de`
- Scope: Task 9 marker recovery and lifecycle consistency
- Critical findings: **0**
- Important findings: **0**
- Minor findings: **0**
- Spec verdict: **PASS**
- Code-quality verdict: **PASS**
- Overall: **READY**

The final empty-marker recovery closes the sole remaining partial-crash gap. The recovery protocol is serialized by the same OS batch lock and a SQLite `BEGIN IMMEDIATE` transaction, mutates only the same validated open descriptor, and commits the marker identity only after durable initialization and revalidation. No Task 9 lifecycle blocker remains in the reviewed scope.

## Final finding closure

### Crash after canonical marker creation and before initialization - closed

When an opener finds an empty canonical marker, it holds the OS lock and proves all of the following before recovery:

- canonical UUID-derived name beneath the stable trusted data root and `.locks` directory;
- exact stable descriptor/name identity;
- non-reparse regular file with one link;
- exact empty contents;
- absent `BatchLockMarkerRecord` under `BEGIN IMMEDIATE`.

`RetentionRepository.bind_empty_lock_marker` rejects an existing registry row before invoking the initializer. The synchronous initializer rechecks the same locked descriptor, writes exactly the content-free byte `0x00`, fsyncs, and repeats type, link, identity, name, size, and byte checks. Only after successful callback return does the repository insert the exact identity and commit.

The crash states are retryable:

- crash before initialization leaves the same empty marker eligible for the strict empty-marker path;
- crash after `0x00` fsync but before DB commit leaves the same initialized marker eligible for the previously reviewed unbound-`0x00` path;
- failure after DB commit retries only through the exact registered identity.

Existing rows, identity mismatches, hard-linked markers, retired `0x01`, oversized/malformed markers, and different same-name objects remain untouched. A live creator holds the OS lock, so a concurrent opener cannot initialize or bind its empty marker.

## Previous lifecycle closures retained

- Windows root/tombstone/nested directories remain pinned and exclusively opened through child scrubbing.
- Multi-link content files are rejected before modification.
- Artifact rollback binds publication identity, size, link count, and SHA-256 on one open descriptor.
- Artifact packaging holds the shared `FileStorage` batch lock through guard acquisition, publish, register, finalization, rollback, and guard release, including after DB-guard expiry.
- Post-register cancellation leaves the available metadata row and ZIP bytes consistent.
- Lock-name replacements are never adopted or mutated on retry.
- Canonical lowercase batch UUID artifact placement and batch-prefixed storage keys remain enforced.
- Early deletion resumes directly in metadata after an injected first metadata-purge failure.
- Exact 30-day and immediate metadata purge remove the marker DB row transactionally while retaining only a content-free, non-adoptable disk `0x01` marker.
- Tombstone retry, bounded deterministic claims, lease recovery, content-before-unavailable ordering, safe purge order, and content-free retained metadata remain covered.
- No Task 10 endpoint/auth/MCP work was introduced.

## Verification evidence

Focused lifecycle suite:

```text
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py tests/test_artifacts.py tests/test_file_intake.py tests/test_remote_fetch.py -p no:cacheprovider
136 passed in 5.05s
```

Cross-process batch-lock contention:

```text
20/20 consecutive runs passed
```

Fresh repository-wide gate:

```text
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider
673 passed, 5 skipped in 9.63s

.\.venv\Scripts\python.exe -m pip check
No broken requirements found.

.\.venv\Scripts\python.exe -m compileall -q src tests
exit 0, no output

git diff --check 6c3969d..HEAD
exit 0, no output
```

Patch package validation before this report edit:

```text
git apply --check --reverse .superpowers/sdd/review-933222e..69021de.diff
exit 0
package Git blob: 73ec6386bac52235c51ea4090b41636a2353c6d3
```

## Disposition

Task 9B is **READY** for controller integration. No Critical, Important, or Minor finding remains in the bounded retention/artifact lifecycle scope. No push, deployment, or Task 10 work was performed.
