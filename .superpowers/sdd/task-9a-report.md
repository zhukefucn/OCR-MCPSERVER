# Task 9A implementation report

## Status and scope

Implementation status: DONE.

Task 9A delivers deterministic Markdown regeneration, immutable and download-safe ZIP publication, SQLite artifact indexing, content-free replacement-audit metadata, artifact limit settings, and the narrow Task 8 `PACKAGING`/`PUBLISHING` adapter. It intentionally does not implement retention content deletion, metadata purge, early deletion, cleanup claims/leases, REST/MCP/auth/download routes, or any Task 10+ behavior.

## RED/GREEN evidence

All Python commands used the repository Python 3.11 virtual environment (`.\.venv\Scripts\python.exe`) because the host `python.exe` is the Windows Store launcher.

### Initial artifact contracts and services

- RED command: `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_artifacts.py`
- RED result: collection failed with `ImportError: cannot import name 'ArtifactErrorCode' from 'ocr_mcp_server.domain'`. This was the expected missing-contract failure before production code existed.
- First GREEN command: `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_artifacts.py`
- First GREEN result: `12 passed`.
- Covered positive/Boolean-rejecting limits, conservative V2 rendering, Unicode/spacing/escaping, table/formula validation, unknown-node omission warnings, Markdown byte limits, deterministic ZIP bytes/order/metadata, exact Task 7 bytes, explicit MinerU files, referenced-image-only inclusion, immutable retry/conflict behavior, SQLite registration/ordered reads, content-free audit metadata, and Task 8 progress counters.

### Boundary hardening RED/GREEN

- RED command: `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_artifacts.py -q`
- RED result: `3 failed, 13 passed` for unknown mapping nodes without `content`, forged audit bytes with updated caller hashes, and unsafe model identifiers reaching the manifest.
- GREEN result for the same command after the minimal fixes: `16 passed`.
- A later ordered-read RED failed with `TypeError: ArtifactRepository.list_for_batch() got an unexpected keyword argument 'result_version'`.
- GREEN added deterministic batch/file/version filters for artifact and audit reads.

### Independent review RED/GREEN

Independent review initially found six Critical/Important-equivalent issues and reproduced changed-image acceptance. The findings were treated as RED requirements:

1. Referenced images were hashed but not bound to Task 7 audit hashes.
2. Total ZIP size was rejected only after the archive had been fully staged.
3. Path-based staging/publication left a descendant directory-swap window.
4. Exact retry could accumulate a different audit-ID set.
5. Direct repository registration could retain unsafe model-version strings.
6. Ordinary text could emit raw HTML metacharacters.

Fixes bind every manifest image pointer to its deterministic Task 7 audit hash, enforce the archive limit inside every write, use a flat server-generated key under a pinned artifact root, bind a content-free audit-set digest/count in the archive contract and SQLite row, validate deterministic audit/candidate/record IDs and safe model identifiers at both boundaries, and HTML-escape non-table text.

The first closure review found one remaining retry race because existing ZIP hash and manifest checks used separate opens. That was fixed by verifying size, SHA-256, manifest bytes, and final name identity through one no-follow/nonblocking descriptor.

The second closure review found the equivalent stage-name race. Final publication now uses anonymous `O_TMPFILE` plus `linkat(AT_EMPTY_PATH)` from the still-open descriptor on POSIX. Windows uses a `CreateFileW` stage handle that denies delete/rename sharing. Both platforms bind the published target identity and size to the still-open staged file before returning. A deterministic post-link identity-swap regression proves the service fails safely instead of returning success.

- Final focused command: `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_artifacts.py`
- Final focused result: `20 passed`.
- Final independent review verdict: READY; no Critical or Important findings.

No test uses GPU, network, MinerU, Paddle, external services, or real sleeps.

### Controller re-review closure

The controller's post-verification review reopened four contracts. Each was reproduced before implementation:

1. Task 7 hashes and caller records could be recomputed around an unaudited final-manifest mutation.
2. Model metadata still admitted path/client-file tokens.
3. Windows cleanup-disposition failure was checked only after publication and could leave a named stage.
4. An exact retry verified the existing ZIP but skipped the directory durability barrier and could reach SQLite registration.

Task 7 closure reconstructs the only permitted final manifest from the exact canonical original plus every ordered audit pointer, decision, reason, original snapshot, and replacement snapshot. One exhaustive, default-deny semantic validator owns every `ReplacementReason` combination. It rejects missing, extra/duplicate real-node coverage, stale snapshots/references, impossible `ALREADY_STRUCTURED`, retained records carrying replacement snapshots, replacement kind/reason mismatches, and any unaudited final mutation.

The shared domain grammar now accepts only bounded ASCII model/version tokens and rejects Windows/POSIX/UNC paths, drive/colon forms, dot segments, whitespace/content, client-file keys/values, and disguised filename tokens such as `acme.pdf.v1`. Both the ZIP boundary and first SQLite insert use the same validator.

Windows now probes handle-bound cleanup before exposing a target, publishes by no-replace handle rename, flushes the pinned writable directory handle, and uses legacy plus extended disposition fallbacks. If both disposition APIs fail before publication, the finalizer never attempts racy path deletion: while the exclusive descriptor is still held it truncates and fsyncs the exact stage inode, then closes it unconditionally and returns failure. If Python `ftruncate` fails, the same handle is truncated with `SetFilePointerEx` plus `SetEndOfFile`. A zero-byte placeholder may remain, but no unindexed archive content remains and an injected replacement object is never deleted. Ordinary publication, interruption, single-cleanup-failure, exact-retry, and post-link-failure paths enumerate no named stage.

Every successful path, including exact retry, removes its retry stage before a required root-directory durability barrier. A planted first barrier failure leaves the exact target for reconciliation but reaches no repository call; the retry repeats and passes the barrier before the sole registration call.

- Task 7 authenticity RED: `4 failed, 1 passed`; GREEN: `5 passed` for unaudited mutation plus stale/missing/duplicate/retained-snapshot cases.
- Model grammar RED: collection initially failed because the shared validator did not exist; later filename-key and disguised-filename regressions each failed as expected before their fixes.
- Windows cleanup RED: the capability-failure test returned success; native cleanup failure left a content-bearing `.artifact-stage-*`; and the initial path fallback had a name-swap race. Handle cleanup paths now remove the stage, while terminal double-disposition failure leaves only a verified zero-byte placeholder and never invokes path unlink.
- Retry barrier RED: the exact retry returned after only one fsync call; a stricter follow-up proved its stage deletion occurred after the barrier. Both are GREEN, with stage deletion before the second barrier and repository calls `0` then `1`.
- Current focused command: `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_artifacts.py`.
- Current focused result: `48 passed`.

## Delivered design

### Domain and safe errors

- Immutable `ArtifactBundle`, `ArtifactSnapshot`, and `ReplacementAuditMetadataSnapshot` contracts.
- Deterministic content-free audit-set SHA-256 binding.
- Stable artifact input/source/image/limit/publish/index codes with content-free messages and discarded causes.

### Markdown and ZIP publication

- Final Markdown is regenerated exclusively from the final canonical V2 page/node list; original MinerU Markdown is never parsed or regex-edited.
- Known title/text/list/code nodes use explicit string fields with deterministic Markdown and HTML escaping.
- Valid table HTML and interline LaTeX are revalidated and rendered as blocks.
- Image nodes use generated `images/NNNNNN.ext` logical paths. Unknown mapping nodes are omitted with `artifact_markdown_node_unsupported` while remaining in `content_list_v2.json`.
- The ZIP uses fixed order, timestamp, stored compression, Unix platform/permissions, safe ASCII/UTF-8 names, no duplicate/case-colliding names, and no data descriptors.
- Exact final/original/audit canonical bytes, available explicitly named MinerU files, and only referenced images are included. Images are deduplicated by SHA-256 and their live bytes must match Task 7 audit bindings.
- `artifact_manifest.json` records identities, versions, ordered non-self entries and their sizes/hashes, counts, engine/model identifiers, warnings/errors, caller times, the content-free audit digest/count, and the SQLite archive-hash binding. The non-circular archive SHA-256 and artifact-manifest SHA-256 live in the returned bundle and SQLite index.
- Publication stages in bounded chunks, fsyncs files/directories where supported, and atomically publishes without replacement. Exact retries verify the complete existing archive and manifest. Conflicts fail safely.
- The final canonical V2 is independently reconstructed from the original and exact Task 7 audit semantics; Task 7 hashes are bindings, not authority for arbitrary caller-supplied mutations.

### SQLite index and audit metadata

- `artifacts` persists deterministic ID, batch/file/version identity, server-generated relative key, media type, size, archive/manifest/audit hashes, audit count, timestamps, availability/deletion state, and optimistic version.
- `replacement_audit_metadata` contains only deterministic IDs, batch/file/source/output identity, image hash, enums/numeric confidence, safe engine/model identifiers, and timestamp. It never stores node snapshots, OCR/recognized content, JSON pointers, page/node indexes, client filenames, or absolute paths.
- `BEGIN IMMEDIATE` registration is atomic and exactly idempotent. It rejects conflicting artifact fields or audit sets and can reconcile missing exact audit rows after an already-published archive.
- Reads are bounded by caller filters and deterministically ordered for batch/file/version consumption by Task 10.

### Settings and Task 8 adapter

- Defaults: 1 GiB artifact, 256 MiB entry, 20,000 entries, and 256 MiB generated Markdown; every limit is positive and rejects Booleans.
- `ArtifactPackagingStep` reports `PACKAGING` byte counters followed by `PUBLISHING` item counters and returns the immutable indexed artifact snapshot. It emits no transport URL.

## Files

New production files:

- `src/ocr_mcp_server/domain/artifacts.py`
- `src/ocr_mcp_server/services/artifacts.py`
- `src/ocr_mcp_server/infra/artifact_repository.py`

Extended production/config files:

- `src/ocr_mcp_server/domain/errors.py`
- `src/ocr_mcp_server/domain/__init__.py`
- `src/ocr_mcp_server/infra/task_models.py`
- `src/ocr_mcp_server/infra/__init__.py`
- `src/ocr_mcp_server/services/__init__.py`
- `src/ocr_mcp_server/settings.py`
- `config/example.yaml`

Tests:

- `tests/test_artifacts.py`

## Commit

- `b8e0380` — `feat: publish deterministic indexed artifacts`
- `951993d` — `docs: record task 9a artifact verification`
- The final verification evidence update to this report is committed separately and its commit is the final Task 9A HEAD reported to the controller.

No push, sync, deployment, image build, parent-plan edit, or external mutation was performed.

## Verification

Pre-implementation-commit gate:

1. `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider`
   - Exit 0: `601 passed, 5 skipped in 6.29s`.
2. `.\.venv\Scripts\python.exe -m pip check`
   - Exit 0: `No broken requirements found.`
3. `.\.venv\Scripts\python.exe -m compileall -q src tests`
   - Exit 0 with no output.
4. `git diff --check`
   - Exit 0 with no whitespace errors.

Post-report gate from `951993d`:

1. `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider`
   - Exit 0: `601 passed, 5 skipped in 6.68s`.
2. `.\.venv\Scripts\python.exe -m pip check`
   - Exit 0: `No broken requirements found.`
3. `.\.venv\Scripts\python.exe -m compileall -q src tests`
   - Exit 0 with no output.
4. `git diff --check 6c3969d..HEAD`
   - Exit 0 with no output.
5. `git status --short`
   - Exit 0 with no output; the worktree was clean.

Post-controller-closure pre-commit gate:

1. `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider`
   - Exit 0: `622 passed, 5 skipped in 6.64s` (before the final three review regressions; a fresh final gate follows the closure commit).
2. `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_artifacts.py`
   - Exit 0: `48 passed in 0.97s`.

Final controller-closure gate:

1. `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider`
   - Exit 0: `629 passed, 5 skipped in 7.20s`.
2. Independent isolated focused run
   - Exit 0: `48 passed`; final verdict `READY` with no Critical or Important findings.

## Review closure and remaining concerns

- Independent review completed the original three rounds and all controller-closure rounds. The final verdict is `READY` with no Critical or Important findings.
- POSIX immutable publication deliberately requires `O_TMPFILE` and `linkat(AT_EMPTY_PATH)`. If the deployment filesystem lacks that Ubuntu-targeted capability, publication fails safely with no insecure named-stage fallback.
- The SQLite MVP still uses `create_all` rather than an Alembic migration. Existing local databases must be recreated to receive the new tables/columns.
- Artifact-manifest and archive hashes cannot be self-embedded without circularity; SQLite and the returned immutable result are the authoritative bindings.
- Retention content deletion, unavailability marking, metadata purge, early deletion, cleanup ownership/concurrency, and their settings/tests remain intentionally deferred to Task 9B.

## Linux publication fallback closure (2026-07-22)

The Ubuntu container accepted `O_TMPFILE` but rejected `linkat(..., AT_EMPTY_PATH)`
with `ENOENT`. A same-filesystem syscall probe established that linking the same open
descriptor through `/proc/self/fd/<fd>` with `AT_SYMLINK_FOLLOW` succeeds and remains
descriptor-bound. The publisher now tries that form only after the anonymous direct
link is rejected with `ENOENT`, `EPERM`, or `EACCES`.

When the filesystem rejects `O_TMPFILE` with a supported-capability error, staging
falls back to a random root-relative file created under the pinned artifact-root
descriptor with `O_CREAT | O_EXCL | O_NOFOLLOW`, mode `0600`. Publication uses
`renameat2(..., RENAME_NOREPLACE)` under the same pinned root. Post-publication inode
checks reject both pre-publication stage-name replacement and late target-name
replacement; failure scrubs the exact open stage descriptor so verified archive
content is not exposed through an attacker-moved name.

TDD regressions cover:

- a forced `AT_EMPTY_PATH` restriction followed by successful proc-descriptor linking;
- forced `O_TMPFILE` `EOPNOTSUPP` followed by named-stage publication;
- a named-stage swap before publication, proving failure and descriptor scrubbing;
- a same-size target swap after the first identity check, proving no false success.

Final verification:

1. Windows artifact suite: `56 passed, 4 skipped in 1.80s`.
2. Ubuntu container artifact suite: `56 passed, 4 skipped in 1.34s`.
3. Windows full suite: `673 passed, 9 skipped in 10.05s`.
4. Ubuntu container full suite: `671 passed, 10 skipped, 1 failed in 9.03s`.
   The sole failure is the separately scoped retention regression
   `test_owned_root_deletion_leaks_and_preserves_a_concurrent_name_replacement`;
   all artifact tests pass.
5. `pip check`: `No broken requirements found.`
6. `compileall -q src tests`: exit 0.
7. `git diff --check`: exit 0 (line-ending conversion warnings only).

No push or deployment was performed.
