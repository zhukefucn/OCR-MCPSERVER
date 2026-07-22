# Task 9B implementation report

## Status and scope

Implementation status: DONE_WITH_CONCERNS.

Task 9B delivers the transport-independent retention repository, durable two-phase cleanup claims, 24-hour content cleanup, 30-day content-free metadata purge, immediate trusted-service whole-task deletion, safe owned-root deletion, cleanup cadence/batch settings, and focused tests. It reuses the Task 2/8 batch/task records and Task 9A artifact/audit index. It does not add REST, MCP, auth, download, scheduler-loop, broker, model, or other Task 10+ behavior.

## RED/GREEN evidence

All Python commands used the repository Python 3.11 virtual environment (`.\.venv\Scripts\python.exe`) because the host `python.exe` is the Windows Store launcher.

### Initial retention contracts

- RED: `.\.venv\Scripts\python.exe -m pytest tests/test_retention.py -p no:cacheprovider`
- Expected result: collection failed because `RetentionErrorCode` and the retention contracts/repository/service did not exist.
- First GREEN: the same command passed `7 passed` after the minimal two-phase repository and service were implemented.

The initial tests covered exact 24-hour and 30-day boundaries, configurable retention values, bounded deterministic work, lease exclusivity and recovery, partial content failure/retry, artifact availability ordering, metadata preservation/purge, early deletion, canonical UUID/root confinement, missing content, symlink rejection, and cadence settings.

### Deletion race and safe-error hardening

- RED: the expanded retention run reported `3 failed, 7 passed`: a missing configured root was not idempotent, a name-swap race moved the replacement without restoring it, and an unsafe planted `OSError` remained reachable through `__context__`.
- GREEN: `10 passed` after missing content became success, mismatched staged objects were restored/leaked rather than deleted, and safe failures were raised outside the unsafe exception context.
- A final-directory replacement regression then failed because deletion did not call a handle-bound identity gate.
- GREEN: Windows final regular-file/directory removal now opens the staged object with delete access, checks the handle identity, and sets disposition on that exact handle. A swapped name is not deleted.

### Artifact-retention integration

- RED: `test_repository_extends_batch_content_retention_to_artifact_expiry` observed the original batch `content_due_at` instead of the later immutable artifact expiry.
- GREEN: artifact registration now transactionally extends the batch content due time and rejects registration after content deletion.

### Settings and purge coverage

- RED: the example YAML omitted cleanup batch, lease, and cadence fields.
- GREEN: the defaults are explicit in `config/example.yaml` and Boolean numeric values are rejected.
- Final focused coverage includes concurrent SQLite claim calls, lease-expiry reclaim without sleeps, retained replacement-audit metadata and stage events before day 30, explicit referentially safe deletion at the boundary, exact early deletion, retry after partial root removal, nested content removal, path/UUID/symlink/reparse defenses, name-swap preservation, and content-free error chains.

No test uses GPU, network, MinerU, Paddle, external services, or real sleeps.

## Delivered design

### Durable retention state and claims

- Each newly created batch gets one `retention` row in the same creation transaction.
- `content_due_at` uses the maximum configured input/intermediate/result retention period; `metadata_due_at` uses the configured audit-metadata period.
- Exact artifact registration extends content retention to a later artifact expiry without reviving an early-deleted or content-deleted task.
- `RetentionRepository.claim_due` uses `BEGIN IMMEDIATE`, deterministic due-time/batch-ID ordering, a caller limit capped at 1,000, unique lease tokens, and reclaim at the exact lease boundary.
- Content and metadata phases cannot be claimed together for one batch. A crashed cleaner becomes reclaimable after lease expiry; a handled failure clears the lease for retry and retains only a stable content-free error code.

### Two-phase cleanup and early deletion

- Content cleanup deletes the complete canonical UUID batch directory beneath both trusted roots. Missing roots/directories are idempotent success.
- Only after both deletions return successfully does one SQLite transaction mark all available artifact rows unavailable, set `deleted_at`, and increment their optimistic versions.
- The metadata row, replacement-audit metadata, artifact index, stage events, file tasks, and batch remain until the exact 30-day boundary.
- Metadata purge explicitly deletes replacement audits, artifact rows, stage events, file tasks, retention state, and the batch in referentially safe order in one transaction.
- `delete_task` is a trusted-service call that makes both phases immediately due and executes the same content and metadata paths. Repeating it after purge returns `False` without touching the filesystem.

### Owned-root deletion

- Targets must be a canonical lowercase UUID direct child of an absolute trusted root. The root itself, traversal, malformed/empty IDs, symlink/junction/reparse targets, and special files are rejected.
- The implementation never calls `shutil.rmtree`. It enumerates deterministically, never follows links, checks parent/child identities before and after staging, and restores or leaks a mismatched staged object rather than deleting it.
- On Windows, final deletion is by the already-open file/directory handle after identity verification, so a concurrent name replacement is not the deleted object.
- Failures discard filesystem paths, filenames, planted content, and unsafe causes/chains.

## Files

New production files:

- `src/ocr_mcp_server/domain/retention.py`
- `src/ocr_mcp_server/infra/retention_repository.py`
- `src/ocr_mcp_server/services/retention.py`

Extended production/config files:

- `src/ocr_mcp_server/domain/errors.py`
- `src/ocr_mcp_server/domain/__init__.py`
- `src/ocr_mcp_server/infra/task_models.py`
- `src/ocr_mcp_server/infra/task_repository.py`
- `src/ocr_mcp_server/infra/artifact_repository.py`
- `src/ocr_mcp_server/infra/__init__.py`
- `src/ocr_mcp_server/services/file_storage.py`
- `src/ocr_mcp_server/services/__init__.py`
- `src/ocr_mcp_server/settings.py`
- `config/example.yaml`

Tests:

- `tests/test_retention.py`
- `tests/test_artifacts.py`

## Commits

- `b2b8993` — `feat: enforce two-phase task retention`
- The report and final verification evidence are committed separately.

No push, sync, deployment, image build, parent-plan edit, or external mutation was performed.

## Verification

Pre-implementation-commit gate:

1. `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider`
   - Exit 0: `642 passed, 5 skipped in 7.73s`.
2. `.\.venv\Scripts\python.exe -m pip check`
   - Exit 0: `No broken requirements found.`
3. `.\.venv\Scripts\python.exe -m compileall -q src tests`
   - Exit 0 with no output.
4. `git diff --check`
   - Exit 0 with no whitespace errors (Git emitted only line-ending conversion notices).

## Review closure and remaining concerns

- SQLite schema management still uses `create_all`, as in Tasks 2/9A. Existing databases need a migration or recreation before the new `retention` table can be used.
- Artifact cleanup assumes the Task 9A bundler is called with the canonical batch-scoped artifact directory (`<artifact-root>/<batch UUID>`). The ZIP format and Task 9A storage key were intentionally not changed in this bounded task.
- Windows handle-bound deletion was exercised in this workspace. POSIX uses no-follow opens/identity-checked same-parent staging and requires final verification on the Ubuntu target by the controller; no deployment/sync was authorized here.
