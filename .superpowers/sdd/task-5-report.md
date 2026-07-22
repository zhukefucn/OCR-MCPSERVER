# Task 5 Report — MinerU candidate extraction and secondary-OCR contracts

## Status and commit range

- Status: complete
- Base commit: `d7a7d857a6b28ed66419aae68a01a73d45465c97`
- Verified implementation head: `1df2a50e46fd3da52d088c29a189daf26facb2c3`
- Implementation commit: `1df2a50 feat: collect secondary OCR image candidates`
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
