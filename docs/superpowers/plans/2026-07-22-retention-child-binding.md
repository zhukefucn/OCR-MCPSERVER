# Retention Child Name-Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make regular-file retention scrubbing fail before truncation whenever the pinned parent child name no longer identifies the opened expected descriptor.

**Architecture:** Carry the already validated parent descriptor and child name into `_scrub_open_regular`. A shared assertion binds the current no-follow name, expected identity, and open descriptor immediately before `ftruncate` and again after `fsync`, supplementing Windows handle-final-path checks and preserving single-link enforcement.

**Tech Stack:** Python 3.11, POSIX `dir_fd`/`O_NOFOLLOW`, Windows file handles, pytest.

## Global Constraints

- Use strict systematic diagnosis and RED/GREEN TDD.
- A pre-truncate mismatch must leave both the moved original and replacement untouched.
- Preserve hard-link rejection, Windows exclusive parent handles, tombstone retry state, and all Task 9 lifecycle closures.
- No push, deployment, Task 10, network, GPU, or external-service work.

---

### Task 1: Bind the current child name to the opened descriptor

**Files:**
- Modify: `src/ocr_mcp_server/services/retention.py`
- Modify: `tests/test_retention.py`

**Interfaces:**
- Consumes: `_scrub_regular(path, expected, *, parent_descriptor, name)` and its pinned traversal context.
- Produces: `_scrub_open_regular(descriptor, expected, path, *, parent_descriptor, name)` with pre/post name binding.
- Produces: `_assert_regular_binding(descriptor, expected, path, *, parent_descriptor, name, require_empty)`.

- [x] **Step 1: Verify the existing Ubuntu regression is RED**

Run on Linux:

```bash
.venv/bin/python -m pytest tests/test_retention.py::test_owned_root_deletion_leaks_and_preserves_a_concurrent_name_replacement -q -p no:cacheprovider
```

Expected: FAIL because `moved-original.zip` is truncated to `b''` while the replacement remains unchanged.

- [x] **Step 2: Preserve the injection while extending the internal contract**

Update test subclasses to accept and forward `parent_descriptor` and `name` as keyword-only arguments:

```python
def _scrub_open_regular(
    self, descriptor, expected, path, *, parent_descriptor, name
):
    # existing injection
    return super()._scrub_open_regular(
        descriptor,
        expected,
        path,
        parent_descriptor=parent_descriptor,
        name=name,
    )
```

Do not alter the failure code or byte-preservation assertions.

- [x] **Step 3: Add the shared pre/post binding assertion**

The helper must inspect `fstat(descriptor)` and the current named entry. On POSIX:

```python
named = os.stat(
    name,
    dir_fd=parent_descriptor,
    follow_symlinks=False,
)
```

On Windows, use `os.lstat(path)` plus the existing final handle-path comparison. Require both objects to be non-reparse regular files with identity `expected`, require descriptor link count one, and when `require_empty=True` require descriptor size zero.

- [x] **Step 4: Call the assertion immediately around mutation**

In `_scrub_open_regular`:

```python
self._assert_regular_binding(
    descriptor,
    expected,
    path,
    parent_descriptor=parent_descriptor,
    name=name,
    require_empty=False,
)
os.ftruncate(descriptor, 0)
os.fsync(descriptor)
self._assert_regular_binding(
    descriptor,
    expected,
    path,
    parent_descriptor=parent_descriptor,
    name=name,
    require_empty=True,
)
```

- [x] **Step 5: Verify GREEN and safety regressions**

Run on Linux:

```bash
.venv/bin/python -m pytest tests/test_retention.py::test_owned_root_deletion_leaks_and_preserves_a_concurrent_name_replacement tests/test_retention.py -k 'hardlink or name_replacement or parent_replacement' -q -p no:cacheprovider
```

Run on Windows:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py -k 'hardlink or name_replacement or parent_replacement or windows_exclusive' -q -p no:cacheprovider
```

Expected: all selected tests pass; Windows-only tests retain their platform skips on Linux.

---

### Task 2: Gates, report, and local commits

**Files:**
- Append: `.superpowers/sdd/task-9b-report.md`

**Interfaces:**
- Consumes: exact Linux RED/GREEN and Linux/Windows gate output.
- Produces: final controller evidence without replacing the current report.

- [x] **Step 1: Run focused Linux and Windows gates**

Linux:

```bash
.venv/bin/python -m pytest tests/test_retention.py -p no:cacheprovider
.venv/bin/python -m pytest -p no:cacheprovider
```

Windows:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_retention.py tests/test_artifacts.py tests/test_file_intake.py tests/test_remote_fetch.py -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider
```

- [x] **Step 2: Run auxiliary verification**

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q src tests
git diff --check
```

- [x] **Step 3: Append evidence and commit locally**

Append root cause, RED symptom, binding invariant, GREEN results, and no-push/no-deploy disposition. Commit implementation/tests separately from plan/report documentation.

- [x] **Step 4: Request same-reviewer review**

Send commit hashes and both-platform evidence to the same controller reviewer for a final bounded review.
