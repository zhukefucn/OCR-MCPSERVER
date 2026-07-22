# Task 5 Report — MinerU candidate extraction and secondary-OCR contracts

## Status and commit range

- Status: complete
- Base commit: `d7a7d857a6b28ed66419aae68a01a73d45465c97`
- Initial verified implementation head: `1df2a50e46fd3da52d088c29a189daf26facb2c3`
- Implementation commit: `1df2a50 feat: collect secondary OCR image candidates`
- Controller-fix base: `a20e494e4d2571138cdc7831239aa2af402b46fb`
- Hardened implementation head: `b5b62ebdbf35c4d627bdacaf99751313520a1efd`
- Focused fix commit: `b5b62eb fix: harden secondary OCR candidate contracts`
- This report is committed separately after the verified implementation so it can cite the immutable implementation hash.

## Files changed

- `src/ocr_mcp_server/domain/secondary_ocr.py` — immutable candidate/reference/record/result/provider/collection contracts and deterministic IDs.
- `src/ocr_mcp_server/domain/errors.py` — safe candidate-collection error codes and failure type.
- `src/ocr_mcp_server/domain/__init__.py` — intentional public contract exports.
- `src/ocr_mcp_server/services/candidate_collection.py` — V2 traversal, path confinement, image inspection, deduplication, standalone-image merging, and pending-record creation.
- `src/ocr_mcp_server/services/__init__.py` — collector export.
- `tests/test_secondary_ocr_domain.py` — immutable contract, strict validation, deterministic record, provider, and collection-invariant tests.
- `tests/test_candidate_collection.py` — real manifest/image, deduplication, path security, race/fault, raster-format, multi-frame, standalone, and safe-error tests.

No dependency, configuration, migration, persistence, orchestration, inference, replacement, REST, MCP, Docker, Ubuntu, parent design, or parent plan file changed.

## Design choices

### Immutable domain model

- `CandidateReference` distinguishes structured MinerU nodes from an explicit standalone-input reference; standalone references cannot contain fake page/node indexes.
- `ImageCandidate` copies sequence inputs to tuples and validates all runtime scalar/enum/path/reference fields.
- `SecondaryOcrResult` has explicit kind, orthogonal angle, optional content plus format, confidence, engine, immutable copied model-version mapping, and valid/invalid/uncertain/failed state.
- `SecondaryOcrProvider` exposes one async `recognize(candidate)` operation; engine identity belongs to the provider rather than the call.
- `CandidateCollection` enforces ordered one-to-one candidate/record coverage, unique IDs, task/version consistency, and a computed reference count.

### Determinism and deduplication

- Candidate and record IDs are SHA-256 namespace digests scoped by local file-task ID, result version, and content hash. They contain no filename, path, or recognized content.
- Manifest nodes remain in page/node order. Repeated raw paths are inspected once; different safe paths with identical content hashes become ordered aliases of one candidate.
- Every reference is retained, and node types are preserved only as ordered hints.
- A standalone PNG/JPEG is always added when supplied. Its trusted intake hash, size, and dimensions are checked against the current accessible regular image; identical MinerU/standalone bytes infer once.

### Path and file safety

- Paths are parsed as relative POSIX paths and reject absolute, drive, UNC, backslash, empty, dot, dot-dot, colon/device, trailing-dot/space, Unicode control/format/surrogate, and normalized-alias cases.
- POSIX opens from the filesystem anchor and traverses every absolute-root and candidate directory component descriptor-relatively with `O_DIRECTORY | O_NOFOLLOW`, then opens the file from the anchored parent descriptor.
- Windows opens the final object with `FILE_FLAG_OPEN_REPARSE_POINT`, rejects reparse targets, and compares the file handle's normalized final path with the exact expected path below `images_directory`. This prevents intermediate junction/symlink escapes even when pathname observations race.
- Initial name identity, open descriptor identity, final descriptor identity, final anchored name identity, byte count, timestamps, inode/file index, and size must agree.
- All open/fstat/fdopen/read/seek/name-stat/close failures are converted to stable failures without retaining unsafe exception context.

### Image validation

- SHA-256 and byte count are streamed in 64 KiB chunks.
- Pillow decompression-bomb warnings are errors; the file is verified, reopened on the same descriptor, and every frame is loaded.
- Positive dimensions, decoded-format/extension agreement, per-frame pixel bounds, and aggregate multi-frame pixel bounds are enforced.
- Supported decoded formats are PNG, JPEG, JPEG 2000, WebP, GIF, BMP, and TIFF when the installed Pillow build supports them.

## RED/GREEN TDD evidence

All production behavior was introduced after a corresponding failing test.

1. Domain contracts:
   - RED: `python -m pytest tests/test_secondary_ocr_domain.py -vv` — 8 expected failures because the contracts/exports did not exist.
   - GREEN: same focused file — 8 passed.
2. Collector baseline:
   - RED: `python -m pytest tests/test_candidate_collection.py -vv` — 47 expected failures because the collector module did not exist; the dependency guard was the sole pass.
   - GREEN: same focused file — 48 passed.
3. Invariant and strictness increments:
   - Record-order RED: one constructor case did not raise; GREEN brought the combined suite to 57 passed.
   - Mutable bbox/boolean-version RED: bbox retained caller mutation; GREEN brought the combined suite to 58 passed.
   - Missing candidate node type RED: returned the wrong path failure instead of a manifest failure; GREEN brought the combined suite to 59 passed.
   - Existing C1-control filename RED: collection succeeded; GREEN brought the combined suite to 60 passed.
   - Huge finite-number edge RED: bbox and confidence raised raw `OverflowError`; GREEN brought the combined suite to 61 passed.
4. Independent-review repairs:
   - RED combined run: 5 failures reproduced invalid runtime enums/scalars, an intermediate-directory outside-root race, a raw fstat fault, an oversized later TIFF frame, and request-supplied engine override.
   - GREEN combined run: 66 passed after descriptor/handle confinement, frame bounds, safe descriptor errors, and strict contracts.
   - Descriptor lifecycle RED: injected `fdopen` `ValueError` escaped raw while fstat/close cases were safe; GREEN combined run: 68 passed.
   - POSIX ancestor-anchor RED: fake descriptor trace showed `images_directory` opened as one pathname; GREEN required filesystem-anchor traversal and produced 69 focused passes.
5. Final focused command:
   - `.venv\Scripts\python.exe -m pytest tests\test_secondary_ocr_domain.py tests\test_candidate_collection.py -q`
   - Result: 69 passed.

## Independent review and closure

The first read-only review found a critical pathname/open race plus important multi-frame, descriptor-fault, runtime-contract, Unicode-control, and adversarial-test gaps.

The three material issue groups requested for closure are all closed:

1. Path confinement/races — closed with POSIX filesystem-anchor component traversal, Windows final-handle containment, ownership-safe descriptor cleanup, and deterministic intermediate/ancestor race tests.
2. Decode and descriptor safety — closed with every-frame plus aggregate pixel bounds and safe fstat/fdopen/close fault conversion with no retained context.
3. Exact contracts/configured engine — closed with strict runtime enum/scalar/path/reference/status validation and collector rejection of non-enum engine input.

Unicode C1 controls and the missing adversarial cases were also closed. The final independent re-review reported: all prior Critical and Important findings closed, no release blocker, `READY`.

## Exact final verification

The repository virtual environment was activated so the exact brief commands used Python 3.11.15.

1. `python -m pytest`
   - Exit code: 0
   - Result: `324 passed, 5 skipped in 3.19s`
2. `python -m pip check`
   - Exit code: 0
   - Result: `No broken requirements found.`
3. `python -m compileall -q src tests`
   - Exit code: 0
   - Result: no output.
4. `git diff --check`
   - Exit code: 0
   - Result: no whitespace errors; Git printed only existing Windows LF-to-CRLF working-copy notices for three tracked files.

## Self-review

- Structured traversal uses only `content.image_source.path`; Markdown, regex, and file-order inference are absent.
- Unknown future node types are collected; non-candidate nodes are ignored; malformed candidate schema/bbox fails safely.
- Same path and same-content aliases converge while all references survive in deterministic order.
- Empty non-image results are allowed; supplied standalone images cannot produce an empty plan.
- Candidate/record IDs and safe errors do not contain filenames, paths, image bytes, manifest JSON, or recognized content.
- No logs were added.
- No MinerU, Paddle, PaddleX, Torch, OpenCV, GPU, network, or remote-service dependency/test requirement was added.
- Scope stops before Task 6 inference and before orchestration, persistence, replacement, REST, or MCP.

## Linux/Windows differences and concerns

- Linux/POSIX confinement uses descriptor-relative traversal beginning at the filesystem root; the algorithm is covered on Windows by a fake-descriptor call-trace test, while native POSIX execution was not available in this run.
- Windows confinement was exercised natively, including final-handle path validation and intermediate directory-symlink escape rejection. Symlink tests skip only on hosts where link creation privilege is unavailable.
- The five full-suite skips are existing platform-specific tests. No Task 5 functional failure remains.
- Multi-frame images are intentionally bounded by the same configured pixel budget in aggregate as well as per frame. This is conservative and prevents GIF/TIFF decode amplification.

## Controller-review follow-up

An independent controller review after the initial Task 5 delivery found three Important issues. They were repaired in the focused range `a20e494e4d2571138cdc7831239aa2af402b46fb..b5b62ebdbf35c4d627bdacaf99751313520a1efd`.

### Files changed in the focused fix

- `src/ocr_mcp_server/services/candidate_collection.py`
- `src/ocr_mcp_server/domain/secondary_ocr.py`
- `tests/test_candidate_collection.py`
- `tests/test_secondary_ocr_domain.py`

### Issue closure and security boundaries

1. JSON and Pillow ordinary-exception normalization — closed.
   - Real 1,200-level JSON nesting reproduces `RecursionError` and a 5,000-digit JSON integer reproduces Python 3.11's integer-limit `ValueError`; both now become context-free `candidate_manifest_invalid` failures.
   - Non-standard Pillow `RuntimeError` with planted path/text now becomes a context-free `candidate_image_invalid_or_unsupported` failure.
   - Both boundaries explicitly re-raise `CandidateCollectionFailure`, then catch only ordinary `Exception`; they do not catch `BaseException`.
2. Windows 8.3 short-root alias — closed.
   - The expected candidate path is expanded with `GetLongPathNameW`.
   - The confinement root is canonicalized from a trusted directory handle with reparse-point rejection and `GetFinalPathNameByHandleW`.
   - The final candidate file handle must still match the canonical expected path and remain below the canonical root, preserving final-handle escape and reparse defenses.
3. `SecondaryOcrResult` valid-state semantics — closed.
   - `VALID + UNCERTAIN` is forbidden.
   - Valid tables require non-empty HTML; valid formulas require non-empty LaTeX.
   - Valid `OTHER` results cannot carry replacement content, making original-node preservation explicit.

### Follow-up RED/GREEN evidence

- RED focused run: 10 failures — six invalid valid-state combinations were accepted; deep JSON leaked `RecursionError`; the oversized integer leaked `ValueError`; Pillow leaked planted `RuntimeError`; and a simulated Windows 8.3 alias was rejected.
- GREEN focused command: `.venv\Scripts\python.exe -m pytest tests\test_secondary_ocr_domain.py tests\test_candidate_collection.py -q`
- GREEN focused result: 80 passed.
- Focused read-only re-review: all three controller findings correct, no blocker, `READY`.

### Follow-up full verification

1. `python -m pytest`
   - Exit code: 0
   - Result: `335 passed, 5 skipped in 2.97s`
2. `python -m pip check`
   - Exit code: 0
   - Result: `No broken requirements found.`
3. `python -m compileall -q src tests`
   - Exit code: 0
   - Result: no output.
4. `git diff --check`
   - Exit code: 0
   - Result: no whitespace errors; Git printed only Windows LF-to-CRLF working-copy notices for the four focused files.

### Follow-up concerns

- The 8.3 alias regression is deterministic through mocked canonical Win32 path results. Normal Windows handle containment and real symlink escape tests continue to run natively.
- No remaining controller-review blocker was identified.

## Interrupt-cleanup follow-up

A subsequent review found that four cleanup-and-rethrow handlers had been narrowed from `BaseException` to `Exception`, allowing interruption signals to bypass descriptor/HANDLE cleanup. The focused fix range is `a1ad8b95916b2ab68d5ebb80b21fd902a25204d3..5cd91d029ab1a3f3ce37e541505268b2c0144a4b`.

- Focused fix commit: `5cd91d0 fix: close candidate handles on interruption`
- Files changed: `src/ocr_mcp_server/services/candidate_collection.py`, `tests/test_candidate_collection.py`.

### Boundary design

- Exactly four `BaseException` catches remain, all limited to resource-ownership cleanup followed by bare re-raise:
  - POSIX child-directory descriptor validation cleanup;
  - POSIX final-file descriptor/name-stat cleanup;
  - POSIX current parent descriptor cleanup;
  - Windows native HANDLE cleanup when ownership transfer to a Python file descriptor is interrupted.
- These blocks close the owned resource and preserve the original `KeyboardInterrupt`, `SystemExit`, or cancellation exception unchanged.
- Business boundaries are intentionally different: manifest parsing and Pillow decoding still preserve `CandidateCollectionFailure` and normalize only ordinary `Exception`. They do not catch `BaseException`.

### RED/GREEN evidence

- RED selected run: two failures and one pass.
  - POSIX `KeyboardInterrupt` left both parent and child descriptors unclosed.
  - Windows HANDLE conversion lacked a cleanup ownership helper.
  - The business-boundary non-swallowing test already passed.
- GREEN selected run: 3 passed.
- GREEN focused command: `.venv\Scripts\python.exe -m pytest tests\test_secondary_ocr_domain.py tests\test_candidate_collection.py -q`
- GREEN focused result: 83 passed.
- Read-only re-review confirmed exactly four cleanup-only `BaseException` catches, unchanged interruption identity, closed POSIX/Windows resources, ordinary-`Exception` business boundaries, no blocker, `READY`.

### Full verification

1. `python -m pytest`
   - Exit code: 0
   - Result: `338 passed, 5 skipped in 3.11s`
2. `python -m pip check`
   - Exit code: 0
   - Result: `No broken requirements found.`
3. `python -m compileall -q src tests`
   - Exit code: 0
   - Result: no output.
4. `git diff --check`
   - Exit code: 0
   - Result: no whitespace errors; only Windows LF-to-CRLF working-copy notices for the two focused files.

### Remaining concerns

- None blocking. POSIX cleanup is covered deterministically with fake descriptors on Windows; Windows HANDLE ownership cleanup is tested independently of the Win32 open call.
