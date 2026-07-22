# Task 9B Marker Recovery and Purge Design

## Goal

Close the two remaining Task 9B lifecycle gaps without weakening the six previously verified ownership and locking protections: recover after a crash between creating a fresh batch-lock marker and committing its first registry binding, and remove per-task marker registry metadata at the existing 30-day purge boundary.

## Recovery invariant

An unregistered on-disk marker may be adopted only when every one of these conditions is proven while holding the same open descriptor and OS batch lock:

- the requested batch ID and marker filename are the canonical lowercase UUID form;
- the marker is a non-reparse regular file with link count exactly one;
- its complete contents are exactly the one-byte fresh marker `0x00`;
- the configured data root, `.locks` parent, marker name, and open descriptor retain the same identities before and after validation;
- the registry transaction finds no `batch_lock_markers` row for the batch.

`FileStorage` owns the filesystem proof and passes an explicit recovery authorization into `RetentionRepository.bind_lock_marker`. The repository performs a `BEGIN IMMEDIATE` transaction and inserts only if the row is still absent. If a row exists, only its exact stored identity is accepted; recovery authorization never overrides or rewrites a mismatch.

The retired marker remains the distinct one-byte value `0x01`. It is therefore not eligible for unbound recovery after registry metadata is purged. Empty, oversized, malformed, linked, reparse, renamed, parent-swapped, and same-name replacement objects are rejected without mutation.

## Crash behavior

The recoverable failure boundary is after exclusive marker creation and durable write of `0x00`, but before the first registry commit. A retry opens that marker once without following links, acquires its OS lock, proves the recovery invariant, and atomically registers its identity.

A failure after the registry commit already has durable identity state. Retry follows the normal exact-identity path and does not use adoption. No filesystem bytes are normalized before authorization.

## Metadata purge

`RetentionRepository.purge_metadata` deletes the batch's marker registry row inside the existing metadata-purge transaction. Deletion remains referentially ordered: audit/artifact/event/file dependents first, then the independent marker row and retention row, then the batch row. The ordinary exactly-30-day path and immediate early-delete path use the same repository method.

The content-free on-disk retired marker may remain after DB purge. Because its byte is `0x01`, a later operation with no registry row rejects it as non-fresh and cannot adopt or mutate it.

## Error handling

Filesystem proof failures continue to surface as the existing stable unsafe-path/cleanup-ownership errors with no path or content disclosure. SQL failures remain claim conflicts or metadata-purge failures through the existing repository mappings. Failed recovery leaves both the marker bytes and any mismatched registry row unchanged.

## Test strategy

Strict RED/GREEN tests will cover:

1. injected first-bind failure after a fresh marker is durably created, followed by a successful retry that registers the same identity;
2. rejection without mutation for malformed unbound marker bytes and same-name replacement/identity mismatch;
3. retry after a simulated post-commit failure using the existing exact registry identity, proving the adoption path is not needed;
4. presence of the batch's `batch_lock_markers` row before the 30-day boundary and deletion exactly at it, while the retired on-disk byte remains `0x01` and non-adoptable;
5. the same row deletion for immediate early metadata purge;
6. all six prior closure regressions, focused lifecycle suites, and the repository-wide gate.

## Scope

No schema beyond the existing `batch_lock_markers` table is added. Canonical artifact placement, metadata-phase resume, deletion timing, external APIs, Task 10 work, push, and deployment remain unchanged.
