# Task 9B Binding Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make retention cleanup and artifact reconciliation mutate only exact server-owned objects while all writers and cleanup share one durable batch lock.

**Architecture:** `FileStorage.batch_lock` becomes a two-part barrier: an OS lock plus a retention-row binding to the exact marker identity. Artifact packaging and input intake both hold it across their full write/commit windows. Cleanup traverses only pinned single-owner objects, and artifact rollback verifies the publication identity, size, and digest on one open descriptor before scrubbing.

**Tech Stack:** Python 3.11, asyncio, Windows handle APIs, POSIX `dir_fd`/`O_NOFOLLOW`, SQLAlchemy async SQLite, pytest/pytest-asyncio.

## Global Constraints

- Use strict RED/GREEN TDD for every finding.
- Preserve canonical `<artifact base>/<batch UUID>` placement and metadata-phase early-delete resume.
- Never follow symlinks/reparse points or truncate multi-link files.
- Never mutate an unknown or replacement lock marker.
- No push, deployment, network, GPU, or external-service work.

---

### Task 1: Durable lock-marker identity and shared batch barrier

**Files:**
- Modify: `src/ocr_mcp_server/infra/task_models.py`
- Modify: `src/ocr_mcp_server/infra/task_repository.py`
- Modify: `src/ocr_mcp_server/infra/retention_repository.py`
- Modify: `src/ocr_mcp_server/services/file_storage.py`
- Modify: `src/ocr_mcp_server/services/file_intake.py`
- Modify: `src/ocr_mcp_server/services/retention.py`
- Test: `tests/test_retention.py`
- Test: `tests/test_file_intake.py`

**Interfaces:**
- Produces: a repository callback that atomically binds/verifies `(st_dev, st_ino)` for the open lock marker.
- Produces: `FileStorage.batch_lock(..., marker_registry=..., allow_retired=...)` that does not mutate before identity authorization.
- Consumes: canonical batch UUID and the existing retention claim/write-guard state.

- [ ] Add a failing retry regression that swaps the retired lock name and proves the second cleanup attempt preserves replacement bytes.
- [ ] Run that regression and confirm the replacement is currently truncated to `0x00`.
- [ ] Add durable marker identity columns and transactional bind/verify behavior.
- [ ] Refactor batch-lock acquisition to create-or-open without mutation, acquire the OS lock, authorize the open identity, then normalize only the authorized descriptor.
- [ ] Run lock/intake/retention focused tests and confirm GREEN.

### Task 2: One OS lock for artifact packaging and cleanup

**Files:**
- Modify: `src/ocr_mcp_server/services/artifacts.py`
- Test: `tests/test_artifacts.py`
- Test: `tests/test_retention.py`

**Interfaces:**
- Consumes: mandatory `FileStorage` batch-lock provider and marker registry.
- Produces: packaging critical section ordered as OS lock -> DB content-write guard -> publish -> register/rollback -> guard release -> OS unlock.

- [ ] Add a failing deterministic test that pauses packaging beyond DB guard expiry and proves cleanup must remain blocked on the OS lock.
- [ ] Run the test and confirm cleanup currently accepts quiescence while packaging remains active.
- [ ] Make the lock provider mandatory on `ArtifactPackagingStep` and hold it across acquire/publish/register/rollback/release.
- [ ] Run artifact/retention focused tests and confirm GREEN.

### Task 3: Exact artifact publication reconciliation

**Files:**
- Modify: `src/ocr_mcp_server/domain/artifacts.py`
- Modify: `src/ocr_mcp_server/services/artifacts.py`
- Test: `tests/test_artifacts.py`

**Interfaces:**
- Produces: `ArtifactBundle.publication_identity` captured from the published archive.
- Consumes: expected publication identity, `size_bytes`, and `sha256` in `scrub_published`.

- [ ] Add a failing regression that swaps the published ZIP before registration failure and proves rollback currently truncates the replacement.
- [ ] Run the regression and confirm RED.
- [ ] Open the archive once no-follow, compare the descriptor identity to `publication_identity`, stream-verify size/digest, truncate that descriptor, and verify final name binding.
- [ ] Run reconciliation regressions and confirm GREEN.

### Task 4: Windows stable traversal and hard-link confinement

**Files:**
- Modify: `src/ocr_mcp_server/services/file_storage.py`
- Modify: `src/ocr_mcp_server/services/retention.py`
- Test: `tests/test_retention.py`

**Interfaces:**
- Produces: Windows directory handles that deny delete/rename sharing during traversal and helpers for handle final path/link count.
- Consumes: persisted tombstone identity and pinned parent chain.

- [ ] Add a failing Windows parent-replacement regression at the child-open boundary.
- [ ] Add a failing external-hardlink regression and confirm external bytes are currently truncated.
- [ ] Pin the configured root, reopen the isolated tombstone without delete sharing, and retain each parent handle across enumeration/open/scrub/verification.
- [ ] Reject any open regular file whose link count is not exactly one before truncation; recheck after scrub.
- [ ] Run adversarial retention tests and confirm replacements/external links are untouched.

### Task 5: Registration commit boundary

**Files:**
- Modify: `src/ocr_mcp_server/services/artifacts.py`
- Test: `tests/test_artifacts.py`

**Interfaces:**
- Produces: packaging state that distinguishes pre-register rollback from post-register committed behavior.

- [ ] Add a failing post-register cancellation regression asserting the available row still matches nonzero archive bytes.
- [ ] Run it and confirm the current archive is scrubbed after commit.
- [ ] Track registration commit and scrub only while uncommitted; propagate post-commit failure without touching the archive.
- [ ] Run the focused test and confirm GREEN.

### Task 6: Verification, report, and commits

**Files:**
- Modify: `.superpowers/sdd/task-9b-report.md`

**Interfaces:**
- Consumes: all focused RED/GREEN outputs.
- Produces: second-remediation evidence and clean commits for controller re-review.

- [ ] Run focused retention/artifact/intake suites.
- [ ] Run repository-wide pytest, `pip check`, `compileall`, and `git diff --check`.
- [ ] Append exact RED/GREEN and final gate output to the report.
- [ ] Commit implementation/tests, then report/plan separately; verify clean status and do not push.
