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

## Ubuntu child-name binding follow-up

An Ubuntu-only retention regression at head `619b65d` revealed one remaining regular-file name race after the READY review. This follow-up preserves the prior review and records the bounded correction.

### RED and root cause

`test_owned_root_deletion_leaks_and_preserves_a_concurrent_name_replacement` opens `result.zip`, then injects this race inside `_scrub_open_regular`: rename the open object to `moved-original.zip`, create replacement bytes at the original `result.zip` name, and call the real scrubber.

Ubuntu observed:

```text
expected: cleanup_ownership_invalid; replacement and moved original preserved
actual: replacement preserved; moved original truncated to b''
```

POSIX traversal opened the child relative to its pinned parent descriptor, but `_scrub_regular` discarded `parent_descriptor` and `name` when invoking `_scrub_open_regular`. The real scrubber validated only `fstat(descriptor)`. After the rename, that descriptor still correctly identified the moved original, remained regular, and had link count one, so every predicate passed and `ftruncate` destroyed its bytes. The later directory check detected the name change only after mutation. Windows did not reproduce the byte loss because its separate handle-final-path check rejected the moved descriptor before truncation.

### Binding correction

`_scrub_regular` now carries the pinned parent descriptor and validated child name into `_scrub_open_regular`. A shared assertion runs immediately before `ftruncate` and again after `fsync`:

- `fstat(descriptor)` must identify the expected non-reparse regular single-link object;
- POSIX resolves the current child name with `os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)`;
- Windows resolves the current name with `lstat(path)` and retains its handle-final-path check;
- the current named entry must be a non-reparse regular file whose identity equals both `expected` and the open descriptor;
- the post-fsync descriptor must additionally be exactly zero bytes.

A missing, renamed, or replaced name is therefore an ownership failure before destructive mutation. The Ubuntu injection reaches the pre-truncate assertion after performing its swap, so neither the replacement nor moved original is scrubbed. Single-link rejection and Windows exclusive parent/handle binding remain part of the same assertion path.

### Verification

The source Ubuntu RED was supplied by the controller. This Windows host has no configured WSL distribution and no Docker or Podman runtime, so it cannot honestly claim a local Ubuntu GREEN; the same reviewer must run the unchanged regression on Ubuntu against the committed head.

Windows exact regression and related name/link/parent set:

```text
test_owned_root_deletion_leaks_and_preserves_a_concurrent_name_replacement: passed
hardlink/name_replacement/parent_replacement/windows_exclusive selection: passed
tests/test_retention.py: 36 passed
```

Windows focused lifecycle gate:

```text
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py tests/test_artifacts.py tests/test_file_intake.py tests/test_remote_fetch.py -p no:cacheprovider
138 passed, 5 skipped in 5.22s
```

Windows repository-wide gate:

```text
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider
675 passed, 10 skipped in 10.32s

.\.venv\Scripts\python.exe -m pip check
No broken requirements found.

.\.venv\Scripts\python.exe -m compileall -q src tests
exit 0, no output

git diff --check
exit 0 (line-ending notices only)
```

### Follow-up disposition

The implementation and Windows gates are ready. Task 9B should remain pending only until the same controller reviewer confirms the unchanged Ubuntu regression and Linux full gate on the committed head. No push, deployment, or Task 10 work is included.

## Ubuntu verification at `5671b6e`

The controller synchronized the clean committed head `5671b6e` to Ubuntu and reran the unchanged regression in a Python 3.11.15 Linux test container with the repository `src` tree explicitly selected through `PYTHONPATH`.

```text
pytest -q tests/test_retention.py::test_owned_root_deletion_leaks_and_preserves_a_concurrent_name_replacement
1 passed

pytest -q tests/test_artifacts.py tests/test_retention.py tests/test_file_intake.py
all selected tests passed; 6 Windows-only tests skipped

pytest -q -rs
all repository tests passed; 10 Windows-only tests skipped

python -m pip check
No broken requirements found.

python -m compileall -q src tests
exit 0, no output

git diff --check 6c3969d..HEAD
exit 0, no output
```

The Ubuntu regression now preserves both the moved original and the concurrent same-name replacement. Together with the Windows `675 passed, 10 skipped` gate above, the implementation has fresh evidence on both supported development platforms and is ready for the final same-reviewer disposition.

## Final same-reviewer disposition

The original Task 9B reviewer inspected the bounded range `69021de..5671b6e`, including the parent-descriptor/name binding in `b4ecb1f`, with the fresh Windows and Ubuntu evidence above. Final verdict: **READY**. No Critical, Important, or Minor blocker remains.
