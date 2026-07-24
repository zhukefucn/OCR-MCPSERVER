# Task 11 Whole-page Orientation Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:test-driven-development` and `superpowers:subagent-driven-development`. Every behavior starts with a failing test. Do not push or deploy from an implementation worker.

**Goal:** Implement the customer-confirmed whole-page orientation recovery workflow behind the existing `reparse_with_page_orientation` REST/MCP contract, while preserving the original input and result version and keeping normal parsing free of whole-page rotation.

**Architecture:** Add content-free recovery domain contracts, an atomic SQLite recovery-token repository, a metadata-driven orientation detector port, a handle-safe document corrector, and a transport-neutral recovery coordinator. A token binds one original server-side file, batch, source result version, suspected pages, and expiry. The coordinator resolves requested pages, independently detects each page, refuses an uncertain/no-op repair, creates a corrected immutable input, and submits a new full-pipeline result version. Detection and pipeline execution are injected; clients cannot choose angles, engines, paths, or retry policy.

## Global constraints

- The normal parse path must never call the whole-page detector or corrector.
- Recovery requires a valid, unexpired, single-purpose `recovery_token`; no re-upload and no caller-supplied path.
- API input remains exactly `recovery_token` plus optional unique positive page numbers.
- Page numbers are one-based. A requested page must exist and, when the token carries a suspicion set, must be in that set.
- Each page is detected independently. Only credible non-zero orthogonal angles are repaired.
- Prefer PDF page rotation metadata; normalize `/Rotate` and preserve all unselected pages. Use pixel rotation only for PNG/JPEG or when an injected correction policy explicitly requires raster correction.
- Never modify the original input or artifact. Corrected input and output use a new recovery attempt/result version.
- If all selected pages are `0` or uncertain, return a stable `orientation_uncertain` outcome and do not enqueue a full rerun.
- Repeated submission with the same token and same canonical page set is idempotent. Conflicting page sets fail safely. Concurrent claims permit one writer.
- Tokens are random, opaque, stored only as SHA-256 digests, expire with content retention (maximum 24 hours), and are invalid after content deletion.
- Persistence and logs contain no OCR text, document bytes, filenames, URLs, or raw tokens.
- No PaddleOCR-VL, runtime engine switching, Redis, PostgreSQL, or external queue.

---

## Task 11A: Recovery domain and SQLite token state

**Files:**

- Add `src/ocr_mcp_server/domain/orientation.py`
- Modify `src/ocr_mcp_server/domain/__init__.py`
- Modify `src/ocr_mcp_server/infra/task_models.py`
- Add `src/ocr_mcp_server/infra/orientation_repository.py`
- Add `tests/test_orientation_domain.py`
- Add `tests/test_orientation_repository.py`

- [ ] Define strict content-free contracts for token issue/resolve, page evidence, orthogonal decisions, recovery claim, and terminal recovery snapshot.
- [ ] Add a recovery record keyed by token digest and bound to file ID, batch ID, source result version, page count, canonical suspected pages, expiry, request fingerprint, corrected input version, result batch/version, and stable state/error code.
- [ ] Issue cryptographically random tokens and return the raw token only once; persist only its digest.
- [ ] Atomically claim a valid token, reject expired/deleted/mismatched state, reconcile identical retries, and reject conflicting page selections.
- [ ] Make terminal completion/failure transitions compare-and-set and restart-safe.
- [ ] Prove raw tokens and content never appear in database rows, exceptions, representations, or logs.

## Task 11B: Metadata-driven detection and immutable correction

**Files:**

- Add `src/ocr_mcp_server/services/orientation_recovery.py`
- Add `src/ocr_mcp_server/infra/document_orientation.py`
- Extend `src/ocr_mcp_server/services/file_storage.py` only through handle-anchored, server-named operations
- Add `tests/test_document_orientation.py`
- Add `tests/test_orientation_correction.py`

- [ ] Define an injected async detector protocol returning one content-free decision per page; no public engine/angle controls.
- [ ] Validate complete one-to-one page decisions, legal orthogonal angles, finite confidence, and a configured credibility threshold.
- [ ] Implement PDF correction with `pypdf`: copy to a server-named staged file, update rotation metadata for selected pages, validate page count and encryption state, fsync, and atomically publish without replacement.
- [ ] Implement PNG/JPEG correction with Pillow transpose operations that do not silently change format; strip unsafe metadata and preserve the original input.
- [ ] Reject arbitrary paths, symlinks/reparse points, page overflow, duplicate decisions, uncertain/non-orthogonal results, and in-place output.
- [ ] Prove unselected pages and originals are byte/logically unchanged, and cleanup cannot delete attacker-substituted paths.

## Task 11C: Recovery coordinator and gateway bridge

**Files:**

- Modify `src/ocr_mcp_server/services/orientation_recovery.py`
- Add `src/ocr_mcp_server/api/document_gateway.py` or the smallest existing concrete gateway composition module
- Modify `src/ocr_mcp_server/api/contracts.py` only if a stable outcome field is required by the approved contract
- Add `tests/test_orientation_recovery.py`
- Add/modify `tests/test_rest_api.py`
- Add/modify `tests/test_mcp_api.py`

- [ ] Resolve token to original file/version and canonical pages; omitted pages means the token's suspected pages, never every page by accident.
- [ ] Claim once, detect independently, stop without correction/rerun when no credible non-zero angle exists, and expose only a stable uncertain outcome.
- [ ] Correct into an immutable recovery-version input and submit the entire normal pipeline through an injected runner.
- [ ] Create a new result version/batch reference without overwriting or hiding the source result.
- [ ] Bridge the existing `DocumentGateway.reparse_with_page_orientation` method to the coordinator with monotonic content-free progress.
- [ ] Map invalid/expired token, conflict, uncertainty, unavailable detector, and processing failure to existing safe REST/MCP boundary failures.
- [ ] Prove API/MCP schemas still expose neither angle nor engine and still list exactly three MCP tools.

## Task 11D: Retention, restart, and integration gates

**Files:**

- Modify retention repository/service only as needed to invalidate recovery records before deleting content
- Add/modify retention and restart integration tests
- Append `.superpowers/sdd/task-11-report.md`
- Update `.superpowers/sdd/progress.md` only after controller deployment succeeds

- [ ] Content cleanup makes tokens unusable and removes corrected inputs/results with the batch; 30-day metadata remains content-free.
- [ ] Restart resumes/reconciles a claimed recovery without duplicate correction or duplicate rerun.
- [ ] Run focused orientation/repository/API tests.
- [ ] Run complete Windows suite, `pip check`, `compileall`, and `git diff --check`.
- [ ] Request independent controller review for token secrecy, path safety, immutable versioning, idempotency, retention races, and no-op/uncertain behavior.
- [ ] After review READY, controller follows the mandated cadence: push GitHub, bundle/checksum sync to Ubuntu, Linux full tests, build, immediate authenticated startup smoke, then immutable image pin and deployment record.

## Acceptance evidence

- A token-bound selected PDF page receives the detector-derived orthogonal metadata correction; other pages and the original remain intact.
- PNG/JPEG recovery creates a new corrected input with the expected dimensions/orientation and leaves the original intact.
- Uncertain or all-zero detection performs no correction and starts no parse task.
- Duplicate identical calls converge on one recovery submission; different page selections conflict.
- Expired, unknown, deleted-content, or wrong-version tokens cannot disclose resource existence or paths.
- Source and recovered artifact versions are both queryable and distinct.
- REST/MCP accept only token/pages and expose content-free progress/errors.
- Normal `parse_documents` never invokes the recovery detector/corrector.
