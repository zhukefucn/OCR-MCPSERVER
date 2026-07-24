# Retention Child Name-Binding Design

## Goal

Prevent retention cleanup from scrubbing an opened regular file after its pinned parent entry has been renamed or replaced, while preserving hard-link rejection and Windows exclusive-handle behavior.

## Root cause

POSIX traversal opens a child relative to the pinned parent descriptor, but `_scrub_regular` currently passes only the open descriptor, expected identity, and absolute path into `_scrub_open_regular`. After a concurrent rename, `fstat` still proves that the descriptor is the originally opened object; it does not prove that the original child name still identifies that object. The Windows branch has an additional handle-final-path check, but the POSIX branch truncates the moved object before directory verification detects the name change.

## Binding contract

`_scrub_regular` will pass the pinned `parent_descriptor` and validated child `name` through to `_scrub_open_regular`. Immediately before `ftruncate`, a shared assertion will require:

- the open descriptor is a non-reparse regular file with identity `expected` and link count one;
- the current child name, resolved without following links relative to the pinned parent descriptor on POSIX, is a non-reparse regular file with identity `expected`;
- the named identity equals the open-descriptor identity;
- on Windows, the current absolute name and existing handle-final-path proof identify the same expected object.

Any mismatch raises `cleanup_ownership_invalid` before modifying either the moved original or the replacement. After `ftruncate` and `fsync`, cleanup repeats the descriptor identity/link/size checks and, where the platform permits, the same current-name-to-descriptor binding check.

## Compatibility

The change does not alter directory pinning, tombstone state, deletion order, or retry metadata. Existing single-link enforcement remains mandatory. Windows continues to use exclusive parent handles and final-path verification; the shared named-entry assertion supplements rather than replaces those checks.

## Test strategy

- Retain the Ubuntu regression that renames the opened file, writes a same-name replacement, and requires both byte sequences to remain unchanged with `cleanup_ownership_invalid`.
- Add a direct binding regression that exercises pre-truncate mismatch without relying on later directory verification.
- Re-run hard-link confinement and Windows exclusive-parent/name-race regressions.
- Run the focused retention suite on Linux, the focused lifecycle suite and full suite on Windows, and the available full Linux gate.

## Scope

Only regular-file scrubbing and its tests change. Artifact publication/finalization, marker recovery, retention timing, API behavior, Task 10, push, and deployment remain out of scope.
