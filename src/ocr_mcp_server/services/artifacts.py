"""Deterministic Markdown, immutable ZIP publication, and Task 8 adapter."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
import errno
from hashlib import sha256
import html
import json
import os
from pathlib import Path
import re
import secrets
import stat
import unicodedata
from uuid import UUID
import zipfile
from typing import Protocol

from .._filesystem import is_reparse as _is_reparse
from ..domain.artifacts import (
    ArtifactBundle,
    ArtifactSnapshot,
    replacement_audit_metadata_sha256,
    validate_content_free_model_versions,
)
from ..domain.errors import ArtifactErrorCode, ArtifactFailure
from ..domain.merge import (
    MergePublicationResult,
    ReplacementAuditRecord,
    ReplacementDecision,
    ReplacementReason,
)
from ..domain.mineru import MinerUDocumentResult
from ..domain.models import ProcessingStage, utc_now
from ..domain.progress import ProgressCounters, ProgressUnit
from ..domain.retention import ContentWriteGuard
from ..domain.secondary_ocr import SecondaryResultKind
from .candidate_collection import _open_candidate
from .file_storage import FileStorage
from .structured_content import (
    StructuredContentInvalid,
    StructuredContentLimits,
    validate_formula_latex,
    validate_table_html,
)


_CHUNK_SIZE = 64 * 1024
_WARNING_OMITTED = ArtifactErrorCode.UNSUPPORTED_NODE.value
_MEDIA_TYPE = "application/zip"
_WINDOWS_DEVICES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp", ".tif", ".tiff"})
_TEXT_TYPES = frozenset({"text", "paragraph"})
_TITLE_TYPES = frozenset({"title", "heading"})
_LIST_TYPES = frozenset({"list", "list_item"})
_CODE_TYPES = frozenset({"code", "code_block"})
_KNOWN_NODE_TYPES = _TEXT_TYPES | _TITLE_TYPES | _LIST_TYPES | _CODE_TYPES | frozenset(
    {"image", "table", "equation_interline"}
)


def _fail(code: ArtifactErrorCode) -> None:
    raise ArtifactFailure(code) from None


def _canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


@dataclass(frozen=True, slots=True)
class ArtifactLimits:
    max_artifact_bytes: int = 1024**3
    max_entry_bytes: int = 256 * 1024**2
    max_entry_count: int = 20_000
    max_markdown_bytes: int = 256 * 1024**2

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 1
            for value in (
                self.max_artifact_bytes,
                self.max_entry_bytes,
                self.max_entry_count,
                self.max_markdown_bytes,
            )
        ):
            raise ArtifactFailure(ArtifactErrorCode.LIMIT_EXCEEDED) from None


@dataclass(frozen=True, slots=True)
class MarkdownRenderResult:
    content: bytes
    warning_codes: tuple[str, ...]


def _canonical_json(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _fail(ArtifactErrorCode.INVALID_INPUT)


def _strict_json(value: bytes) -> object:
    def reject_duplicates(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError
            result[key] = item
        return result

    try:
        return json.loads(
            value.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeError):
        _fail(ArtifactErrorCode.INVALID_INPUT)


def _safe_logical_path(value: object) -> tuple[str, ...] | None:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith(("/", "//"))
        or "\\" in value
        or re.match(r"^[A-Za-z]:($|/)", value)
    ):
        return None
    parts = tuple(value.split("/"))
    for part in parts:
        if (
            not part
            or part in {".", ".."}
            or ":" in part
            or part != part.rstrip(" .")
            or part.split(".", 1)[0].upper() in _WINDOWS_DEVICES
            or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in part)
        ):
            return None
    return parts


def _markdown_escape(value: str) -> str:
    return re.sub(
        r"([\\`*_{}\[\]()#+.!|>~-])",
        r"\\\1",
        html.escape(value, quote=False),
    )


def _typed_string(content: object, names: tuple[str, ...]) -> str | None:
    if not isinstance(content, Mapping):
        return None
    for name in names:
        value = content.get(name)
        if isinstance(value, str):
            return value
    return None


def _structured_limits(max_bytes: int) -> StructuredContentLimits:
    utf8 = min(40_000_000, max_bytes)
    characters = min(10_000_000, utf8)
    artifact = min(256 * 1024**2, max(max_bytes, utf8))
    return StructuredContentLimits(
        max_characters=characters,
        max_utf8_bytes=utf8,
        max_html_depth=256,
        max_html_elements=100_000,
        max_table_rows=20_000,
        max_table_cells=100_000,
        max_latex_repetition=1_024,
        max_artifact_bytes=artifact,
    )


def render_markdown(
    manifest: object,
    *,
    image_names: Mapping[str, str],
    max_bytes: int,
) -> MarkdownRenderResult:
    """Render only known V2 node fields; unknown mappings are omitted safely."""

    if type(max_bytes) is not int or max_bytes < 1 or not isinstance(image_names, Mapping):
        _fail(ArtifactErrorCode.INVALID_INPUT)
    if not isinstance(manifest, list):
        _fail(ArtifactErrorCode.INVALID_INPUT)
    limits = _structured_limits(max_bytes)
    pages: list[str] = []
    warned = False
    try:
        for page in manifest:
            if not isinstance(page, list):
                _fail(ArtifactErrorCode.INVALID_INPUT)
            blocks: list[str] = []
            for node in page:
                if not isinstance(node, Mapping):
                    _fail(ArtifactErrorCode.INVALID_INPUT)
                node_type = node.get("type")
                if not isinstance(node_type, str):
                    _fail(ArtifactErrorCode.INVALID_INPUT)
                if node_type not in _KNOWN_NODE_TYPES:
                    warned = True
                    continue
                content = node.get("content")
                if not isinstance(content, Mapping):
                    _fail(ArtifactErrorCode.INVALID_INPUT)
                block: str | None
                if node_type in _TITLE_TYPES:
                    value = _typed_string(content, ("text", "text_content"))
                    if value is None:
                        _fail(ArtifactErrorCode.INVALID_INPUT)
                    block = "# " + _markdown_escape(value)
                elif node_type in _TEXT_TYPES:
                    value = _typed_string(content, ("text", "text_content"))
                    if value is None:
                        _fail(ArtifactErrorCode.INVALID_INPUT)
                    block = _markdown_escape(value)
                elif node_type in _LIST_TYPES:
                    value = _typed_string(content, ("text", "text_content"))
                    if value is None:
                        _fail(ArtifactErrorCode.INVALID_INPUT)
                    block = "- " + _markdown_escape(value)
                elif node_type in _CODE_TYPES:
                    value = _typed_string(content, ("code", "text", "text_content"))
                    if value is None:
                        _fail(ArtifactErrorCode.INVALID_INPUT)
                    block = "\n".join("    " + line for line in value.split("\n"))
                elif node_type == "image":
                    image_source = content.get("image_source")
                    raw_path = image_source.get("path") if isinstance(image_source, Mapping) else None
                    if _safe_logical_path(raw_path) is None or raw_path not in image_names:
                        _fail(ArtifactErrorCode.UNSAFE_IMAGE)
                    logical = image_names[raw_path]
                    if _safe_logical_path(logical) is None or not logical.startswith("images/"):
                        _fail(ArtifactErrorCode.UNSAFE_IMAGE)
                    block = f"![]({logical})"
                elif node_type == "table":
                    block = validate_table_html(content.get("html"), limits)
                elif node_type == "equation_interline":
                    if content.get("math_type") != "latex":
                        _fail(ArtifactErrorCode.INVALID_INPUT)
                    latex = validate_formula_latex(content.get("math_content"), limits)
                    block = f"$$\n{latex}\n$$"
                else:
                    raise AssertionError
                if block is not None:
                    blocks.append(block)
            pages.append("\n\n".join(blocks))
    except ArtifactFailure:
        raise
    except StructuredContentInvalid:
        _fail(ArtifactErrorCode.INVALID_INPUT)
    except BaseException:
        _fail(ArtifactErrorCode.INVALID_INPUT)
    output = ("\n\n---\n\n".join(pages) + "\n").encode("utf-8")
    if len(output) > max_bytes:
        _fail(ArtifactErrorCode.LIMIT_EXCEEDED)
    return MarkdownRenderResult(output, (_WARNING_OMITTED,) if warned else ())


@dataclass(frozen=True, slots=True)
class _FileEntry:
    name: str
    path: Path
    root: Path
    size: int
    digest: str
    error_code: ArtifactErrorCode


@dataclass(frozen=True, slots=True)
class _BytesEntry:
    name: str
    content: bytes
    digest: str


class _ArchiveLimitReached(Exception):
    pass


class _BoundedArchiveWriter:
    def __init__(self, raw, limit: int) -> None:
        self._raw = raw
        self._limit = limit

    def write(self, value: bytes) -> int:
        if self._raw.tell() + len(value) > self._limit:
            raise _ArchiveLimitReached from None
        return self._raw.write(value)

    def __getattr__(self, name: str):
        return getattr(self._raw, name)


def _same_stat(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        stat.S_IFMT(first.st_mode),
        first.st_size,
        first.st_mtime_ns,
        first.st_ctime_ns,
    ) == (
        second.st_dev,
        second.st_ino,
        stat.S_IFMT(second.st_mode),
        second.st_size,
        second.st_mtime_ns,
        second.st_ctime_ns,
    )


def _same_object(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        stat.S_IFMT(first.st_mode),
    ) == (
        second.st_dev,
        second.st_ino,
        stat.S_IFMT(second.st_mode),
    )


def _close_opened(opened) -> None:
    failed = False
    for descriptor in (opened.descriptor, opened.parent_descriptor):
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                failed = True
    if failed:
        raise OSError from None


def _scan_file(path: Path, root: Path, limit: int, code: ArtifactErrorCode) -> tuple[int, str]:
    opened = None
    try:
        opened = _open_candidate(path, confined_root=root)
        initial = os.fstat(opened.descriptor)
        if not stat.S_ISREG(initial.st_mode) or not _same_stat(initial, opened.initial_name_stat) or initial.st_size > limit:
            raise OSError
        digest = sha256()
        size = 0
        while chunk := os.read(opened.descriptor, _CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
            if size > limit:
                raise OSError
        if not _same_stat(initial, os.fstat(opened.descriptor)) or not _same_stat(initial, opened.current_name_stat()):
            raise OSError
        return size, digest.hexdigest()
    except BaseException:
        _fail(code)
    finally:
        if opened is not None:
            try:
                _close_opened(opened)
            except OSError:
                _fail(code)


def _read_file(path: Path, root: Path, limit: int, code: ArtifactErrorCode) -> bytes:
    size, expected = _scan_file(path, root, limit, code)
    opened = None
    try:
        opened = _open_candidate(path, confined_root=root)
        initial = os.fstat(opened.descriptor)
        if not stat.S_ISREG(initial.st_mode) or not _same_stat(initial, opened.initial_name_stat):
            raise OSError
        chunks: list[bytes] = []
        digest = sha256()
        count = 0
        while chunk := os.read(opened.descriptor, _CHUNK_SIZE):
            chunks.append(chunk)
            digest.update(chunk)
            count += len(chunk)
            if count > limit:
                raise OSError
        if (
            count != size
            or digest.hexdigest() != expected
            or not _same_stat(initial, os.fstat(opened.descriptor))
            or not _same_stat(initial, opened.current_name_stat())
        ):
            raise OSError
        return b"".join(chunks)
    except BaseException:
        _fail(code)
    finally:
        if opened is not None:
            try:
                _close_opened(opened)
            except OSError:
                _fail(code)


def _entry_info(name: str) -> zipfile.ZipInfo:
    if _safe_logical_path(name) is None:
        _fail(ArtifactErrorCode.INVALID_INPUT)
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100600 << 16
    info.internal_attr = 0
    return info


def _write_file_entry(archive: zipfile.ZipFile, entry: _FileEntry) -> None:
    opened = None
    try:
        opened = _open_candidate(entry.path, confined_root=entry.root)
        initial = os.fstat(opened.descriptor)
        if not stat.S_ISREG(initial.st_mode) or not _same_stat(initial, opened.initial_name_stat):
            raise OSError
        digest = sha256()
        size = 0
        with archive.open(_entry_info(entry.name), "w", force_zip64=True) as output:
            while chunk := os.read(opened.descriptor, _CHUNK_SIZE):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if (
            size != entry.size
            or digest.hexdigest() != entry.digest
            or not _same_stat(initial, os.fstat(opened.descriptor))
            or not _same_stat(initial, opened.current_name_stat())
        ):
            raise OSError
    except _ArchiveLimitReached:
        raise
    except BaseException:
        _fail(entry.error_code)
    finally:
        if opened is not None:
            try:
                _close_opened(opened)
            except OSError:
                _fail(entry.error_code)


def _validate_directory(path: Path, code: ArtifactErrorCode) -> os.stat_result:
    try:
        value = os.lstat(path)
        if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode) or bool(getattr(value, "st_file_attributes", 0) & 0x400):
            raise OSError
        return value
    except BaseException:
        _fail(code)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _windows_pin_directory(path: Path) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x40000000,
        0x1 | 0x2,  # Deliberately deny delete/rename sharing while publishing.
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise OSError from None

    class _AttributeTagInfo(ctypes.Structure):
        _fields_ = (("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD))

    value = _AttributeTagInfo()
    inspect = kernel32.GetFileInformationByHandleEx
    inspect.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    inspect.restype = wintypes.BOOL
    if not inspect(handle, 9, ctypes.byref(value), ctypes.sizeof(value)) or value.FileAttributes & 0x400:
        kernel32.CloseHandle(handle)
        raise OSError from None
    return int(handle)


class _PinnedArtifactRoot:
    def __init__(self, path: Path, identity: os.stat_result) -> None:
        self.path = path
        self.identity = identity
        self.descriptor: int | None = None
        self.handle: int | None = None

    def __enter__(self):
        try:
            if os.name == "nt":
                self.handle = _windows_pin_directory(self.path)
            else:
                self.descriptor = os.open(
                    self.path,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                if not _same_object(os.fstat(self.descriptor), self.identity):
                    raise OSError
            if not _same_object(os.lstat(self.path), self.identity):
                raise OSError
            return self
        except BaseException:
            self.__exit__(None, None, None)
            _fail(ArtifactErrorCode.PUBLISH_FAILED)

    def __exit__(self, _type, _value, _traceback) -> None:
        if self.descriptor is not None:
            try:
                os.close(self.descriptor)
            except OSError:
                pass
            self.descriptor = None
        if self.handle is not None:
            try:
                ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self.handle)
            except BaseException:
                pass
            self.handle = None

    def create_stage(self) -> tuple[int, str | None, Path | None]:
        if self.descriptor is not None:
            temporary_flag = getattr(os, "O_TMPFILE", 0)
            if temporary_flag:
                try:
                    descriptor = os.open(
                        self.path,
                        os.O_RDWR
                        | temporary_flag
                        | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                    )
                    return descriptor, None, None
                except OSError as exc:
                    if exc.errno not in {
                        errno.EOPNOTSUPP,
                        errno.EINVAL,
                        errno.EISDIR,
                        errno.ENOENT,
                    }:
                        raise
            for _ in range(32):
                name = f".artifact-stage-{secrets.token_hex(16)}.zip"
                try:
                    descriptor = os.open(
                        name,
                        os.O_RDWR
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=self.descriptor,
                    )
                except FileExistsError:
                    continue
                return descriptor, name, self.path / name
            raise OSError from None
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        for _ in range(32):
            name = f".artifact-stage-{secrets.token_hex(16)}.zip"
            path = self.path / name
            handle = create_file(
                str(path),
                0x80000000 | 0x40000000 | 0x00010000,
                0x1 | 0x2,  # Pin the stage name by denying delete/rename sharing.
                None,
                1,
                0x00200000,
                None,
            )
            if handle == wintypes.HANDLE(-1).value:
                if ctypes.get_last_error() in {80, 183}:
                    continue
                raise OSError from None
            try:
                descriptor = msvcrt.open_osfhandle(
                    int(handle), os.O_RDWR | getattr(os, "O_BINARY", 0)
                )
            except BaseException:
                kernel32.CloseHandle(handle)
                raise
            return descriptor, name, path
        raise OSError from None

    def link_no_replace(
        self, stage_descriptor: int, stage_name: str | None, target_name: str
    ) -> None:
        if self.descriptor is None:
            assert stage_name is not None
            import msvcrt

            class _RenameInformation(ctypes.Structure):
                _fields_ = (
                    ("Flags", wintypes.DWORD),
                    ("RootDirectory", wintypes.HANDLE),
                    ("FileNameLength", wintypes.DWORD),
                    ("FileName", wintypes.WCHAR * 1),
                )

            destination = str(self.path / target_name)
            encoded = destination.encode("utf-16-le")
            size = ctypes.sizeof(_RenameInformation) + len(encoded) - ctypes.sizeof(
                wintypes.WCHAR
            )
            buffer = ctypes.create_string_buffer(size)
            information = ctypes.cast(
                buffer, ctypes.POINTER(_RenameInformation)
            ).contents
            information.Flags = 0
            information.RootDirectory = None
            information.FileNameLength = len(encoded)
            ctypes.memmove(
                ctypes.addressof(buffer) + _RenameInformation.FileName.offset,
                encoded,
                len(encoded),
            )
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            rename = kernel32.SetFileInformationByHandle
            rename.argtypes = (
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            )
            rename.restype = wintypes.BOOL
            if rename(
                msvcrt.get_osfhandle(stage_descriptor),
                3,
                buffer,
                size,
            ):
                return
            error_number = ctypes.get_last_error()
            if error_number in {80, 183}:
                raise FileExistsError(error_number, os.strerror(error_number))
            raise OSError(error_number, os.strerror(error_number))
        libc = ctypes.CDLL(None, use_errno=True)
        if stage_name is not None:
            try:
                renameat2 = libc.renameat2
            except AttributeError:
                raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS)) from None
            renameat2.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            renameat2.restype = ctypes.c_int
            if renameat2(
                self.descriptor,
                os.fsencode(stage_name),
                self.descriptor,
                os.fsencode(target_name),
                1,
            ) == 0:
                return
            error_number = ctypes.get_errno()
            if error_number == errno.EEXIST:
                raise FileExistsError(error_number, os.strerror(error_number))
            raise OSError(error_number, os.strerror(error_number))
        linkat = libc.linkat
        linkat.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
        )
        linkat.restype = ctypes.c_int
        if linkat(
            stage_descriptor,
            b"",
            self.descriptor,
            os.fsencode(target_name),
            0x1000,
        ) == 0:
            return
        error_number = ctypes.get_errno()
        if error_number in {errno.ENOENT, errno.EPERM, errno.EACCES}:
            proc_descriptor = os.fsencode(f"/proc/self/fd/{stage_descriptor}")
            if linkat(
                -100,
                proc_descriptor,
                self.descriptor,
                os.fsencode(target_name),
                0x400,
            ) == 0:
                return
            error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError(error_number, os.strerror(error_number))
        raise OSError(error_number, os.strerror(error_number))

    def arm_stage_cleanup(self, stage_descriptor: int, stage_name: str | None) -> None:
        if stage_name is None:
            return
        if self.descriptor is not None:
            return
        self._set_stage_cleanup(stage_descriptor, True)

    def disarm_stage_cleanup(
        self, stage_descriptor: int, stage_name: str | None
    ) -> None:
        if stage_name is None:
            return
        if self.descriptor is not None:
            return
        self._set_stage_cleanup(stage_descriptor, False)

    @staticmethod
    def _set_stage_cleanup(stage_descriptor: int, delete: bool) -> None:
        import msvcrt

        class _Disposition(ctypes.Structure):
            _fields_ = (("DeleteFile", wintypes.BOOL),)

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.SetFileInformationByHandle
        function.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        function.restype = wintypes.BOOL
        disposition = _Disposition(delete)
        if not function(
            msvcrt.get_osfhandle(stage_descriptor),
            4,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise OSError from None

    @staticmethod
    def _set_stage_cleanup_extended(stage_descriptor: int) -> None:
        import msvcrt

        class _DispositionEx(ctypes.Structure):
            _fields_ = (("Flags", wintypes.DWORD),)

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.SetFileInformationByHandle
        function.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        function.restype = wintypes.BOOL
        disposition = _DispositionEx(0x1)
        if not function(
            msvcrt.get_osfhandle(stage_descriptor),
            21,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise OSError from None

    def force_stage_cleanup(
        self, stage_descriptor: int, stage_name: str | None
    ) -> None:
        if stage_name is None:
            return
        if self.descriptor is not None:
            self.scrub_stage(stage_descriptor)
            return
        try:
            self._set_stage_cleanup(stage_descriptor, True)
        except OSError:
            self._set_stage_cleanup_extended(stage_descriptor)

    @staticmethod
    def scrub_stage(stage_descriptor: int) -> None:
        try:
            os.ftruncate(stage_descriptor, 0)
        except OSError:
            if os.name != "nt":
                raise
            import msvcrt

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            seek = kernel32.SetFilePointerEx
            seek.argtypes = (
                wintypes.HANDLE,
                ctypes.c_longlong,
                ctypes.POINTER(ctypes.c_longlong),
                wintypes.DWORD,
            )
            seek.restype = wintypes.BOOL
            truncate = kernel32.SetEndOfFile
            truncate.argtypes = (wintypes.HANDLE,)
            truncate.restype = wintypes.BOOL
            handle = msvcrt.get_osfhandle(stage_descriptor)
            position = ctypes.c_longlong()
            if not seek(handle, 0, ctypes.byref(position), 0) or not truncate(handle):
                raise OSError(ctypes.get_last_error(), "stage scrub failed") from None
        os.fsync(stage_descriptor)
        if os.fstat(stage_descriptor).st_size != 0:
            raise OSError from None

    def fsync(self) -> None:
        if self.descriptor is not None:
            os.fsync(self.descriptor)
        else:
            assert self.handle is not None
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            flush = kernel32.FlushFileBuffers
            flush.argtypes = (wintypes.HANDLE,)
            flush.restype = wintypes.BOOL
            if not flush(self.handle):
                raise OSError(ctypes.get_last_error(), "directory durability failed")

    def _named_stat(self, name: str) -> os.stat_result:
        if self.descriptor is None:
            return os.lstat(self.path / name)
        return os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)

    def scan_descriptor(
        self,
        descriptor: int,
        name: str | None,
        limit: int,
        code: ArtifactErrorCode,
    ) -> tuple[int, str, os.stat_result]:
        try:
            initial = os.fstat(descriptor)
            named = None if name is None else self._named_stat(name)
            if (
                not stat.S_ISREG(initial.st_mode)
                or (named is not None and not _same_stat(initial, named))
                or initial.st_size > limit
            ):
                raise OSError
            os.lseek(descriptor, 0, os.SEEK_SET)
            digest = sha256()
            size = 0
            while chunk := os.read(descriptor, _CHUNK_SIZE):
                size += len(chunk)
                if size > limit:
                    raise OSError
                digest.update(chunk)
            if not _same_stat(initial, os.fstat(descriptor)) or (
                name is not None
                and not _same_stat(initial, self._named_stat(name))
            ):
                raise OSError
            return size, digest.hexdigest(), initial
        except BaseException:
            _fail(code)

    def verify_descriptor(
        self,
        descriptor: int,
        name: str,
        *,
        expected_identity: os.stat_result,
        expected_size: int,
        expected_digest: str,
        expected_manifest: bytes,
        limit: int,
        code: ArtifactErrorCode,
    ) -> os.stat_result:
        try:
            size, digest, initial = self.scan_descriptor(
                descriptor,
                name,
                limit,
                code,
            )
            if (
                not _same_object(expected_identity, initial)
                or size != expected_size
                or digest != expected_digest
            ):
                raise OSError
            os.lseek(descriptor, 0, os.SEEK_SET)
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                with zipfile.ZipFile(source) as archive:
                    if archive.read("artifact_manifest.json") != expected_manifest:
                        raise OSError
            final = os.fstat(descriptor)
            if not _same_stat(initial, final) or not _same_stat(
                initial, self._named_stat(name)
            ):
                raise OSError
            return final
        except ArtifactFailure:
            raise
        except BaseException:
            _fail(code)

    def verify_existing(
        self,
        name: str,
        *,
        expected_size: int,
        expected_digest: str,
        expected_manifest: bytes,
        limit: int,
    ) -> tuple[int, os.stat_result, object | None]:
        opened = None
        descriptor: int | None = None
        success = False
        try:
            if self.descriptor is None:
                opened = _open_candidate(self.path / name, confined_root=self.path)
                descriptor = opened.descriptor
            else:
                descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_NONBLOCK", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=self.descriptor,
                )
            initial = os.fstat(descriptor)
            final = self.verify_descriptor(
                descriptor,
                name,
                expected_identity=initial,
                expected_size=expected_size,
                expected_digest=expected_digest,
                expected_manifest=expected_manifest,
                limit=limit,
                code=ArtifactErrorCode.PUBLISH_CONFLICT,
            )
            success = True
            return descriptor, final, opened
        except ArtifactFailure:
            raise
        except BaseException:
            _fail(ArtifactErrorCode.PUBLISH_CONFLICT)
        finally:
            if not success and opened is not None:
                try:
                    _close_opened(opened)
                except OSError:
                    _fail(ArtifactErrorCode.PUBLISH_CONFLICT)
            elif not success and descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    _fail(ArtifactErrorCode.PUBLISH_CONFLICT)


def _extract_image_references(
    manifests: tuple[object, object]
) -> tuple[tuple[str, str], ...]:
    ordered: list[tuple[str, str]] = []
    for manifest in manifests:
        if not isinstance(manifest, list):
            _fail(ArtifactErrorCode.INVALID_INPUT)
        for page_index, page in enumerate(manifest):
            if not isinstance(page, list):
                _fail(ArtifactErrorCode.INVALID_INPUT)
            for node_index, node in enumerate(page):
                if not isinstance(node, Mapping):
                    _fail(ArtifactErrorCode.INVALID_INPUT)
                content = node.get("content")
                if content is None:
                    continue
                if not isinstance(content, Mapping):
                    _fail(ArtifactErrorCode.INVALID_INPUT)
                source = content.get("image_source")
                if source is None:
                    continue
                raw = source.get("path") if isinstance(source, Mapping) else None
                if _safe_logical_path(raw) is None:
                    _fail(ArtifactErrorCode.UNSAFE_IMAGE)
                reference = (raw, f"/{page_index}/{node_index}")
                if reference not in ordered:
                    ordered.append(reference)
    return tuple(ordered)


def _hash_id(namespace: str, *values: object) -> str:
    return sha256((namespace + "\0" + "\0".join(str(value) for value in values)).encode("utf-8")).hexdigest()


def _safe_model_metadata(
    publication: MergePublicationResult,
) -> tuple[list[str], list[str]]:
    engines: set[str] = set()
    versions: set[str] = set()
    reference_indexes: dict[str, int] = {}
    for record in publication.records:
        if not isinstance(record, ReplacementAuditRecord):
            _fail(ArtifactErrorCode.INVALID_INPUT)
        engine = record.engine.value
        engines.add(engine)
        expected_candidate = "candidate-" + _hash_id(
            "image-candidate",
            publication.task_id,
            publication.source_version,
            record.image_sha256,
        )
        expected_processing = "secondary-" + _hash_id(
            "secondary-record",
            publication.task_id,
            publication.source_version,
            record.image_sha256,
        )
        reference_index = reference_indexes.get(record.candidate_id, 0)
        reference_indexes[record.candidate_id] = reference_index + 1
        expected_audit = "audit-" + _hash_id(
            "merge-audit",
            publication.task_id,
            publication.source_version,
            publication.output_version,
            record.candidate_id,
            reference_index,
            record.reason.value,
        )
        if (
            record.candidate_id != expected_candidate
            or record.processing_record_id != expected_processing
            or record.audit_id != expected_audit
        ):
            _fail(ArtifactErrorCode.INVALID_INPUT)
        try:
            safe_versions = validate_content_free_model_versions(
                record.model_versions
            )
        except ValueError:
            _fail(ArtifactErrorCode.INVALID_INPUT)
        for key, value in safe_versions.items():
            versions.add(f"{key}:{value}")
    return sorted(engines), sorted(versions)


def _expected_audit_bytes(publication: MergePublicationResult) -> bytes:
    return _canonical_json(
        {
            "task_id": publication.task_id,
            "source_version": publication.source_version,
            "output_version": publication.output_version,
            "records": [
                {
                    "audit_id": item.audit_id,
                    "task_id": item.task_id,
                    "source_version": item.source_version,
                    "output_version": item.output_version,
                    "candidate_id": item.candidate_id,
                    "processing_record_id": item.processing_record_id,
                    "image_sha256": item.image_sha256,
                    "json_pointer": item.json_pointer,
                    "page_index": item.page_index,
                    "node_index": item.node_index,
                    "original_node_type": item.original_node_type,
                    "kind": item.kind.value,
                    "angle": int(item.angle),
                    "confidence": item.confidence,
                    "engine": item.engine.value,
                    "model_versions": dict(sorted(item.model_versions.items())),
                    "decision": item.decision.value,
                    "reason": item.reason.value,
                    "timestamp": item.timestamp.isoformat(),
                    "original_node_snapshot": item.original_node_snapshot,
                    "replacement_node_snapshot": item.replacement_node_snapshot,
                }
                for item in publication.records
            ],
        }
    )


def _snapshot_value(value: str | None) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, str):
        _fail(ArtifactErrorCode.INVALID_INPUT)
    parsed = _strict_json(value.encode("utf-8"))
    if not isinstance(parsed, dict):
        _fail(ArtifactErrorCode.INVALID_INPUT)
    canonical = json.dumps(
        parsed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if canonical != value:
        _fail(ArtifactErrorCode.INVALID_INPUT)
    return parsed


def _is_existing_structure(
    node: dict,
    limits: StructuredContentLimits,
) -> bool:
    content = node.get("content")
    if not isinstance(content, dict):
        return False
    try:
        if node.get("type") == "table":
            validate_table_html(content.get("html"), limits)
            return True
        if (
            node.get("type") == "equation_interline"
            and content.get("math_type") == "latex"
        ):
            validate_formula_latex(content.get("math_content"), limits)
            return True
    except StructuredContentInvalid:
        return False
    return False


def _expected_node_for_audit(
    record: ReplacementAuditRecord,
    original_node: dict | None,
    replacement_node: dict | None,
    limits: StructuredContentLimits,
) -> dict | None:
    """Exhaustively validate Task 7 audit semantics; unknown combinations fail."""

    if record.reason is ReplacementReason.STANDALONE_REFERENCE:
        if (
            record.decision is ReplacementDecision.RETAINED
            and original_node is None
            and replacement_node is None
        ):
            return None
        _fail(ArtifactErrorCode.INVALID_INPUT)
    if original_node is None:
        _fail(ArtifactErrorCode.INVALID_INPUT)

    structured = _is_existing_structure(original_node, limits)
    if record.reason in {
        ReplacementReason.REPLACED_TABLE,
        ReplacementReason.REPLACED_FORMULA,
    }:
        if (
            record.decision is not ReplacementDecision.REPLACED
            or replacement_node is None
            or structured
        ):
            _fail(ArtifactErrorCode.INVALID_INPUT)
        original_content = original_node.get("content")
        replacement_content = replacement_node.get("content")
        if not isinstance(original_content, dict) or not isinstance(
            replacement_content, dict
        ):
            _fail(ArtifactErrorCode.INVALID_INPUT)
        expected = deepcopy(original_node)
        expected_content = expected["content"]
        if record.reason is ReplacementReason.REPLACED_TABLE:
            if (
                record.kind is not SecondaryResultKind.TABLE
                or replacement_node.get("type") != "table"
                or not isinstance(replacement_content.get("html"), str)
            ):
                _fail(ArtifactErrorCode.INVALID_INPUT)
            expected["type"] = "table"
            expected_content.pop("math_content", None)
            expected_content.pop("math_type", None)
            expected_content["html"] = replacement_content["html"]
        else:
            if (
                record.kind is not SecondaryResultKind.FORMULA
                or replacement_node.get("type") != "equation_interline"
                or replacement_content.get("math_type") != "latex"
                or not isinstance(replacement_content.get("math_content"), str)
            ):
                _fail(ArtifactErrorCode.INVALID_INPUT)
            expected["type"] = "equation_interline"
            expected_content.pop("html", None)
            expected_content["math_content"] = replacement_content["math_content"]
            expected_content["math_type"] = "latex"
        if replacement_node != expected:
            _fail(ArtifactErrorCode.INVALID_INPUT)
        return replacement_node

    if record.decision is not ReplacementDecision.RETAINED or replacement_node is not None:
        _fail(ArtifactErrorCode.INVALID_INPUT)
    if record.reason is ReplacementReason.ALREADY_STRUCTURED:
        if not structured:
            _fail(ArtifactErrorCode.INVALID_INPUT)
        return original_node
    if record.reason is ReplacementReason.STALE_REFERENCE:
        _fail(ArtifactErrorCode.INVALID_INPUT)
    if structured:
        _fail(ArtifactErrorCode.INVALID_INPUT)
    if record.reason is ReplacementReason.OTHER_IMAGE:
        if record.kind is not SecondaryResultKind.OTHER:
            _fail(ArtifactErrorCode.INVALID_INPUT)
        return original_node
    if record.reason is ReplacementReason.INVALID_CONTENT:
        if record.kind not in {
            SecondaryResultKind.TABLE,
            SecondaryResultKind.FORMULA,
        }:
            _fail(ArtifactErrorCode.INVALID_INPUT)
        return original_node
    if record.reason in {
        ReplacementReason.UNCERTAIN,
        ReplacementReason.FAILED,
        ReplacementReason.INVALID_RESULT,
    }:
        return original_node
    _fail(ArtifactErrorCode.INVALID_INPUT)


def _reconstruct_audited_final(
    original: object,
    publication: MergePublicationResult,
    limits: StructuredContentLimits,
) -> object:
    if not isinstance(original, list):
        _fail(ArtifactErrorCode.INVALID_INPUT)
    expected_pointers = {
        pointer for _raw, pointer in _extract_image_references((original,))
    }
    reconstructed = deepcopy(original)
    seen: set[str] = set()
    for record in publication.records:
        pointer = record.json_pointer
        if pointer is None:
            if (
                record.page_index is not None
                or record.node_index is not None
                or record.original_node_type is not None
                or record.reason is not ReplacementReason.STANDALONE_REFERENCE
            ):
                _fail(ArtifactErrorCode.INVALID_INPUT)
            _expected_node_for_audit(
                record,
                _snapshot_value(record.original_node_snapshot),
                _snapshot_value(record.replacement_node_snapshot),
                limits,
            )
            continue
        if (
            pointer not in expected_pointers
            or pointer in seen
            or type(record.page_index) is not int
            or type(record.node_index) is not int
            or pointer != f"/{record.page_index}/{record.node_index}"
        ):
            _fail(ArtifactErrorCode.INVALID_INPUT)
        seen.add(pointer)
        try:
            original_node = original[record.page_index][record.node_index]
        except (IndexError, TypeError):
            _fail(ArtifactErrorCode.INVALID_INPUT)
        if (
            not isinstance(original_node, dict)
            or record.original_node_type != original_node.get("type")
            or _snapshot_value(record.original_node_snapshot) != original_node
        ):
            _fail(ArtifactErrorCode.INVALID_INPUT)
        replacement = _snapshot_value(record.replacement_node_snapshot)
        reconstructed[record.page_index][record.node_index] = _expected_node_for_audit(
            record,
            original_node,
            replacement,
            limits,
        )
    if seen != expected_pointers:
        _fail(ArtifactErrorCode.INVALID_INPUT)
    return reconstructed


class ArtifactBundler:
    """Verify Task 7 outputs and atomically publish one deterministic stored ZIP."""

    def __init__(self, limits: ArtifactLimits) -> None:
        if not isinstance(limits, ArtifactLimits):
            _fail(ArtifactErrorCode.INVALID_INPUT)
        self._limits = limits

    def publish(
        self,
        result: MinerUDocumentResult,
        publication: MergePublicationResult,
        *,
        artifact_root: Path,
        batch_id: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> ArtifactBundle:
        try:
            return self._publish(result, publication, artifact_root, batch_id, created_at, expires_at)
        except ArtifactFailure:
            raise
        except BaseException:
            _fail(ArtifactErrorCode.PUBLISH_FAILED)

    def _publish(self, result, publication, artifact_root, batch_id, created_at, expires_at):
        if (
            not isinstance(result, MinerUDocumentResult)
            or not isinstance(publication, MergePublicationResult)
            or not isinstance(batch_id, str)
            or not _canonical_uuid(batch_id)
            or result.file_task_id != publication.task_id
            or type(publication.source_version) is not int
            or type(publication.output_version) is not int
            or publication.source_version < 1
            or publication.output_version < 1
            or not isinstance(created_at, datetime)
            or not isinstance(expires_at, datetime)
            or created_at.tzinfo is None
            or expires_at.tzinfo is None
            or expires_at <= created_at
            or not isinstance(artifact_root, Path)
            or not artifact_root.is_absolute()
        ):
            _fail(ArtifactErrorCode.INVALID_INPUT)
        publication_root = publication.publication_directory
        result_root = result.result_root
        publication_identity = _validate_directory(publication_root, ArtifactErrorCode.UNSAFE_SOURCE)
        result_identity = _validate_directory(result_root, ArtifactErrorCode.UNSAFE_SOURCE)
        images_identity = _validate_directory(result.images_directory, ArtifactErrorCode.UNSAFE_IMAGE)
        if publication_root.name != f"version-{publication.output_version:08d}":
            _fail(ArtifactErrorCode.INVALID_INPUT)
        try:
            if {item.name for item in publication_root.iterdir()} != {
                "original_content_list_v2.json",
                "content_list_v2.json",
                "secondary_ocr_audit.json",
                "publication_manifest.json",
            }:
                _fail(ArtifactErrorCode.INVALID_INPUT)
        except ArtifactFailure:
            raise
        except BaseException:
            _fail(ArtifactErrorCode.UNSAFE_SOURCE)
        fixed = {
            "original_content_list_v2.json": publication.original_snapshot_path,
            "content_list_v2.json": publication.manifest_path,
            "secondary_ocr_audit.json": publication.audit_path,
        }
        if any(path.absolute() != (publication_root / name).absolute() for name, path in fixed.items()):
            _fail(ArtifactErrorCode.INVALID_INPUT)
        loaded = {
            name: _read_file(path, publication_root, self._limits.max_entry_bytes, ArtifactErrorCode.UNSAFE_SOURCE)
            for name, path in fixed.items()
        }
        expected_hashes = {
            "original_content_list_v2.json": publication.original_sha256,
            "content_list_v2.json": publication.manifest_sha256,
            "secondary_ocr_audit.json": publication.audit_sha256,
        }
        if any(sha256(loaded[name]).hexdigest() != expected_hashes[name] for name in loaded):
            _fail(ArtifactErrorCode.INVALID_INPUT)
        replacement_count = sum(
            record.decision.value == "replaced" for record in publication.records
        )
        if (
            any(
                record.task_id != publication.task_id
                or record.source_version != publication.source_version
                or record.output_version != publication.output_version
                for record in publication.records
            )
            or replacement_count != publication.replacement_count
            or len(publication.records) - replacement_count != publication.retained_count
            or loaded["secondary_ocr_audit.json"] != _expected_audit_bytes(publication)
        ):
            _fail(ArtifactErrorCode.INVALID_INPUT)
        engines, model_versions = _safe_model_metadata(publication)
        try:
            audit_metadata_digest = replacement_audit_metadata_sha256(
                batch_id, result.file_task_id, publication.records
            )
        except BaseException:
            _fail(ArtifactErrorCode.INVALID_INPUT)
        binding_path = publication_root / "publication_manifest.json"
        binding_bytes = _read_file(binding_path, publication_root, self._limits.max_entry_bytes, ArtifactErrorCode.UNSAFE_SOURCE)
        binding = _strict_json(binding_bytes)
        if (
            not isinstance(binding, dict)
            or set(binding) != {"artifact_kind", "task_id", "source_version", "output_version", "files"}
            or binding.get("artifact_kind") != "merge"
            or binding.get("task_id") != publication.task_id
            or binding.get("source_version") != publication.source_version
            or binding.get("output_version") != publication.output_version
            or binding.get("files") != {name: sha256(value).hexdigest() for name, value in sorted(loaded.items())}
            or _canonical_json(binding) != binding_bytes
        ):
            _fail(ArtifactErrorCode.INVALID_INPUT)
        original = _strict_json(loaded["original_content_list_v2.json"])
        final = _strict_json(loaded["content_list_v2.json"])
        if _canonical_json(original) != loaded["original_content_list_v2.json"] or _canonical_json(final) != loaded["content_list_v2.json"]:
            _fail(ArtifactErrorCode.INVALID_INPUT)
        if _canonical_json(
            _reconstruct_audited_final(
                original,
                publication,
                _structured_limits(self._limits.max_markdown_bytes),
            )
        ) != loaded["content_list_v2.json"]:
            _fail(ArtifactErrorCode.INVALID_INPUT)
        source_original = _read_file(result.content_list_v2_path, result_root, self._limits.max_entry_bytes, ArtifactErrorCode.UNSAFE_SOURCE)
        if sha256(source_original).hexdigest() != publication.original_sha256:
            _fail(ArtifactErrorCode.INVALID_INPUT)

        image_names: dict[str, str] = {}
        image_entries: list[_FileEntry] = []
        digest_to_name: dict[str, str] = {}
        audits_by_pointer: dict[str, str] = {}
        for record in publication.records:
            if record.json_pointer is None:
                continue
            existing_digest = audits_by_pointer.setdefault(
                record.json_pointer, record.image_sha256
            )
            if existing_digest != record.image_sha256:
                _fail(ArtifactErrorCode.INVALID_INPUT)
        expected_by_raw: dict[str, str] = {}
        ordered_raw: list[str] = []
        for raw, pointer in _extract_image_references((final, original)):
            expected_digest = audits_by_pointer.get(pointer)
            if expected_digest is None:
                _fail(ArtifactErrorCode.UNSAFE_IMAGE)
            previous = expected_by_raw.setdefault(raw, expected_digest)
            if previous != expected_digest:
                _fail(ArtifactErrorCode.UNSAFE_IMAGE)
            if raw not in ordered_raw:
                ordered_raw.append(raw)
        for raw in ordered_raw:
            parts = _safe_logical_path(raw)
            assert parts is not None
            path = result.content_list_v2_path.parent.joinpath(*parts).absolute()
            try:
                if not path.is_relative_to(result.images_directory.absolute()):
                    _fail(ArtifactErrorCode.UNSAFE_IMAGE)
            except (OSError, ValueError):
                _fail(ArtifactErrorCode.UNSAFE_IMAGE)
            extension = path.suffix.casefold()
            if extension not in _IMAGE_EXTENSIONS:
                _fail(ArtifactErrorCode.UNSAFE_IMAGE)
            size, digest = _scan_file(path, result.images_directory, self._limits.max_entry_bytes, ArtifactErrorCode.UNSAFE_IMAGE)
            if digest != expected_by_raw[raw]:
                _fail(ArtifactErrorCode.UNSAFE_IMAGE)
            logical = digest_to_name.get(digest)
            if logical is None:
                logical = f"images/{len(image_entries):06d}{extension}"
                digest_to_name[digest] = logical
                image_entries.append(_FileEntry(
                    logical, path, result.images_directory, size, digest,
                    ArtifactErrorCode.UNSAFE_IMAGE,
                ))
            image_names[raw] = logical

        rendered = render_markdown(final, image_names=image_names, max_bytes=self._limits.max_markdown_bytes)
        byte_entries: list[_BytesEntry] = [
            _BytesEntry("final.md", rendered.content, sha256(rendered.content).hexdigest()),
            *(
                _BytesEntry(name, loaded[name], sha256(loaded[name]).hexdigest())
                for name in (
                    "content_list_v2.json",
                    "original_content_list_v2.json",
                    "secondary_ocr_audit.json",
                )
            ),
        ]
        file_entries: list[_FileEntry] = []
        explicit = (
            ("mineru/original.md", result.markdown_path),
            ("mineru/middle.json", result.middle_json_path),
            ("mineru/legacy_content_list.json", result.legacy_content_list_path),
        )
        for name, path in explicit:
            if path is None:
                continue
            size, digest = _scan_file(path, result_root, self._limits.max_entry_bytes, ArtifactErrorCode.UNSAFE_SOURCE)
            file_entries.append(_FileEntry(
                name, path, result_root, size, digest,
                ArtifactErrorCode.UNSAFE_SOURCE,
            ))
        file_entries.extend(image_entries)
        ordered_entries: list[_BytesEntry | _FileEntry] = [*byte_entries, *file_entries]
        names = [entry.name for entry in ordered_entries]
        if (
            len(ordered_entries) + 1 > self._limits.max_entry_count
            or len(names) != len({name.casefold() for name in names})
            or any(_safe_logical_path(name) is None for name in names)
            or any((len(entry.content) if isinstance(entry, _BytesEntry) else entry.size) > self._limits.max_entry_bytes for entry in ordered_entries)
        ):
            _fail(ArtifactErrorCode.LIMIT_EXCEEDED)

        artifact_id = "artifact-" + _hash_id("artifact", batch_id, result.file_task_id, publication.output_version)
        manifest_document = {
            "schema_version": 1,
            "artifact_id": artifact_id,
            "batch_id": batch_id,
            "file_task_id": result.file_task_id,
            "source_version": publication.source_version,
            "result_version": publication.output_version,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "entries": [
                {
                    "name": entry.name,
                    "size_bytes": len(entry.content) if isinstance(entry, _BytesEntry) else entry.size,
                    "sha256": entry.digest,
                }
                for entry in ordered_entries
            ],
            "archive_sha256_binding": "sqlite_artifact_index",
            "replacement_count": publication.replacement_count,
            "retained_count": publication.retained_count,
            "audit_metadata_sha256": audit_metadata_digest,
            "audit_record_count": len(publication.records),
            "engines": engines,
            "model_versions": model_versions,
            "warning_codes": list(rendered.warning_codes),
            "error_codes": [],
        }
        manifest_bytes = _canonical_json(manifest_document)
        if len(manifest_bytes) > self._limits.max_entry_bytes:
            _fail(ArtifactErrorCode.LIMIT_EXCEEDED)
        manifest_digest = sha256(manifest_bytes).hexdigest()
        ordered_entries.append(_BytesEntry("artifact_manifest.json", manifest_bytes, manifest_digest))

        try:
            if (
                not _same_object(publication_identity, os.lstat(publication_root))
                or not _same_object(result_identity, os.lstat(result_root))
                or not _same_object(images_identity, os.lstat(result.images_directory))
            ):
                _fail(ArtifactErrorCode.UNSAFE_SOURCE)
        except ArtifactFailure:
            raise
        except BaseException:
            _fail(ArtifactErrorCode.UNSAFE_SOURCE)

        try:
            artifact_root.mkdir(parents=True, exist_ok=True)
        except BaseException:
            _fail(ArtifactErrorCode.PUBLISH_FAILED)
        global_root = artifact_root
        global_identity = _validate_directory(
            global_root, ArtifactErrorCode.PUBLISH_FAILED
        )
        batch_root = global_root / batch_id
        try:
            batch_root.mkdir(mode=0o700, exist_ok=True)
        except BaseException:
            _fail(ArtifactErrorCode.PUBLISH_FAILED)
        if not _same_object(global_identity, os.lstat(global_root)):
            _fail(ArtifactErrorCode.PUBLISH_FAILED)
        root_identity = _validate_directory(
            batch_root, ArtifactErrorCode.PUBLISH_FAILED
        )
        artifact_root = batch_root
        artifact_name = f"{artifact_id}.zip"
        storage_key = f"{batch_id}/{artifact_name}"
        if (
            not _same_object(global_identity, os.lstat(global_root))
            or not _same_object(root_identity, os.lstat(artifact_root))
        ):
            _fail(ArtifactErrorCode.PUBLISH_FAILED)
        target = artifact_root / artifact_name
        with _PinnedArtifactRoot(artifact_root, root_identity) as pinned:
            try:
                descriptor, stage_name, _stage_path = pinned.create_stage()
            except BaseException:
                _fail(ArtifactErrorCode.PUBLISH_FAILED)
            published = False
            stage_closed = False
            stage_identity = None
            existing_descriptor: int | None = None
            existing_identity: os.stat_result | None = None
            existing_opened = None
            try:
                try:
                    with os.fdopen(descriptor, "w+b", closefd=False) as raw:
                        bounded = _BoundedArchiveWriter(
                            raw, self._limits.max_artifact_bytes
                        )
                        with zipfile.ZipFile(
                            bounded,
                            "w",
                            compression=zipfile.ZIP_STORED,
                            allowZip64=True,
                            strict_timestamps=True,
                        ) as archive:
                            for entry in ordered_entries:
                                if isinstance(entry, _BytesEntry):
                                    archive.writestr(_entry_info(entry.name), entry.content)
                                else:
                                    _write_file_entry(archive, entry)
                        raw.flush()
                        os.fsync(raw.fileno())
                except _ArchiveLimitReached:
                    _fail(ArtifactErrorCode.LIMIT_EXCEEDED)
                size, archive_digest, stage_identity = pinned.scan_descriptor(
                    descriptor,
                    stage_name,
                    self._limits.max_artifact_bytes,
                    ArtifactErrorCode.PUBLISH_FAILED,
                )
                try:
                    if (
                        not _same_object(publication_identity, os.lstat(publication_root))
                        or not _same_object(result_identity, os.lstat(result_root))
                        or not _same_object(images_identity, os.lstat(result.images_directory))
                        or not _same_object(root_identity, os.lstat(artifact_root))
                    ):
                        _fail(ArtifactErrorCode.PUBLISH_FAILED)
                except ArtifactFailure:
                    raise
                except BaseException:
                    _fail(ArtifactErrorCode.PUBLISH_FAILED)
                try:
                    pinned.arm_stage_cleanup(descriptor, stage_name)
                    pinned.disarm_stage_cleanup(descriptor, stage_name)
                except BaseException:
                    _fail(ArtifactErrorCode.PUBLISH_FAILED)
                try:
                    pinned.link_no_replace(descriptor, stage_name, artifact_name)
                    published = True
                except FileExistsError:
                    if stage_name is not None:
                        try:
                            if not _same_stat(
                                stage_identity, pinned._named_stat(stage_name)
                            ):
                                _fail(ArtifactErrorCode.PUBLISH_FAILED)
                        except ArtifactFailure:
                            raise
                        except BaseException:
                            _fail(ArtifactErrorCode.PUBLISH_FAILED)
                    (
                        existing_descriptor,
                        existing_identity,
                        existing_opened,
                    ) = pinned.verify_existing(
                        artifact_name,
                        expected_size=size,
                        expected_digest=archive_digest,
                        expected_manifest=manifest_bytes,
                        limit=self._limits.max_artifact_bytes,
                    )
                    if stage_name is not None:
                        try:
                            pinned.force_stage_cleanup(descriptor, stage_name)
                            os.close(descriptor)
                            stage_closed = True
                        except BaseException:
                            _fail(ArtifactErrorCode.PUBLISH_FAILED)
                except OSError as exc:
                    if exc.errno == errno.EEXIST:
                        _fail(ArtifactErrorCode.PUBLISH_CONFLICT)
                    _fail(ArtifactErrorCode.PUBLISH_FAILED)
                if published:
                    try:
                        published_identity = pinned._named_stat(artifact_name)
                        if (
                            not _same_object(stage_identity, published_identity)
                            or published_identity.st_size != size
                        ):
                            if pinned.descriptor is not None:
                                pinned.scrub_stage(descriptor)
                            _fail(ArtifactErrorCode.PUBLISH_FAILED)
                    except ArtifactFailure:
                        raise
                    except BaseException:
                        _fail(ArtifactErrorCode.PUBLISH_FAILED)
                try:
                    pinned.fsync()
                except BaseException:
                    _fail(ArtifactErrorCode.PUBLISH_FAILED)
                if (
                    not _same_object(global_identity, os.lstat(global_root))
                    or not _same_object(root_identity, os.lstat(artifact_root))
                ):
                    _fail(ArtifactErrorCode.PUBLISH_FAILED)
                final_descriptor = descriptor if published else existing_descriptor
                expected_identity = stage_identity if published else existing_identity
                if final_descriptor is None or expected_identity is None:
                    _fail(ArtifactErrorCode.PUBLISH_FAILED)
                try:
                    final_identity = pinned.verify_descriptor(
                        final_descriptor,
                        artifact_name,
                        expected_identity=expected_identity,
                        expected_size=size,
                        expected_digest=archive_digest,
                        expected_manifest=manifest_bytes,
                        limit=self._limits.max_artifact_bytes,
                        code=(
                            ArtifactErrorCode.PUBLISH_FAILED
                            if published
                            else ArtifactErrorCode.PUBLISH_CONFLICT
                        ),
                    )
                except ArtifactFailure:
                    if published:
                        pinned.scrub_stage(descriptor)
                    raise
                return ArtifactBundle(
                    artifact_id=artifact_id,
                    batch_id=batch_id,
                    file_task_id=result.file_task_id,
                    source_version=publication.source_version,
                    result_version=publication.output_version,
                    storage_key=storage_key,
                    media_type=_MEDIA_TYPE,
                    size_bytes=size,
                    sha256=archive_digest,
                    manifest_sha256=manifest_digest,
                    audit_metadata_sha256=audit_metadata_digest,
                    audit_record_count=len(publication.records),
                    created_at=created_at,
                    expires_at=expires_at,
                    path=target,
                    publication_identity=(final_identity.st_dev, final_identity.st_ino),
                    warning_codes=rendered.warning_codes,
                    replacement_count=publication.replacement_count,
                    retained_count=publication.retained_count,
                )
            finally:
                cleanup_error: BaseException | None = None
                if existing_opened is not None:
                    try:
                        _close_opened(existing_opened)
                    except OSError as exc:
                        cleanup_error = exc
                elif existing_descriptor is not None:
                    try:
                        os.close(existing_descriptor)
                    except OSError as exc:
                        cleanup_error = exc
                if not stage_closed:
                    try:
                        if not published and stage_name is not None:
                            try:
                                pinned.force_stage_cleanup(descriptor, stage_name)
                            except OSError:
                                pinned.scrub_stage(descriptor)
                    except BaseException as exc:
                        cleanup_error = exc
                    finally:
                        try:
                            os.close(descriptor)
                        except OSError as exc:
                            if cleanup_error is None:
                                cleanup_error = exc
                if cleanup_error is not None:
                    raise cleanup_error

    def scrub_published(self, bundle: ArtifactBundle) -> None:
        """Erase archive bytes through a verified open descriptor; keep its name."""

        descriptor: int | None = None
        try:
            batch_root = bundle.path.parent
            name = bundle.path.name
            batch_info = os.lstat(batch_root)
            if (
                Path(name).name != name
                or _is_reparse(batch_info)
                or not stat.S_ISDIR(batch_info.st_mode)
            ):
                _fail(ArtifactErrorCode.PUBLISH_FAILED)
            with _PinnedArtifactRoot(batch_root, batch_info) as pinned:
                expected = (
                    os.stat(name, dir_fd=pinned.descriptor, follow_symlinks=False)
                    if pinned.descriptor is not None
                    else os.lstat(bundle.path)
                )
                if _is_reparse(expected) or not stat.S_ISREG(expected.st_mode):
                    _fail(ArtifactErrorCode.PUBLISH_FAILED)
                if pinned.descriptor is not None:
                    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(name, flags, dir_fd=pinned.descriptor)
                else:
                    descriptor = FileStorage._windows_open_file_descriptor(
                        bundle.path,
                        create=False,
                        delete_access=False,
                        write_access=True,
                    )
                opened = os.fstat(descriptor)
                if (
                    not _same_object(expected, opened)
                    or (opened.st_dev, opened.st_ino) != bundle.publication_identity
                    or opened.st_size != bundle.size_bytes
                    or opened.st_nlink != 1
                ):
                    _fail(ArtifactErrorCode.PUBLISH_FAILED)
                os.lseek(descriptor, 0, os.SEEK_SET)
                digest = sha256()
                size = 0
                while chunk := os.read(descriptor, _CHUNK_SIZE):
                    size += len(chunk)
                    if size > bundle.size_bytes:
                        _fail(ArtifactErrorCode.PUBLISH_FAILED)
                    digest.update(chunk)
                before_scrub = os.fstat(descriptor)
                if (
                    size != bundle.size_bytes
                    or digest.hexdigest() != bundle.sha256
                    or not _same_object(opened, before_scrub)
                    or before_scrub.st_size != bundle.size_bytes
                    or before_scrub.st_nlink != 1
                ):
                    _fail(ArtifactErrorCode.PUBLISH_FAILED)
                os.ftruncate(descriptor, 0)
                os.fsync(descriptor)
                current = (
                    os.stat(name, dir_fd=pinned.descriptor, follow_symlinks=False)
                    if pinned.descriptor is not None
                    else os.lstat(bundle.path)
                )
                if not _same_object(opened, current) or current.st_size != 0:
                    _fail(ArtifactErrorCode.PUBLISH_FAILED)
        except ArtifactFailure:
            raise
        except BaseException:
            _fail(ArtifactErrorCode.PUBLISH_FAILED)
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


class ArtifactRepositoryProtocol(Protocol):
    async def acquire_content_write(
        self,
        batch_id: str,
        file_task_id: str,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> ContentWriteGuard: ...

    async def register(
        self,
        bundle: ArtifactBundle,
        records: tuple[ReplacementAuditRecord, ...],
        *,
        write_guard: ContentWriteGuard,
    ) -> ArtifactSnapshot: ...

    async def release_content_write(self, guard: ContentWriteGuard) -> None: ...


class ArtifactPackagingStep:
    """Narrow Task 8 adapter: package bytes, then publish one metadata item."""

    def __init__(
        self,
        bundler: ArtifactBundler,
        repository: ArtifactRepositoryProtocol,
        *,
        batch_locks: FileStorage,
        marker_registry,
        now_factory=utc_now,
        write_lease_seconds: int = 300,
    ) -> None:
        self._bundler = bundler
        self._repository = repository
        self._batch_locks = batch_locks
        self._marker_registry = marker_registry
        self._now_factory = now_factory
        self._write_lease_seconds = write_lease_seconds

    async def run(
        self,
        result: MinerUDocumentResult,
        publication: MergePublicationResult,
        *,
        artifact_root: Path,
        batch_id: str,
        created_at: datetime,
        expires_at: datetime,
        progress,
        cancellation,
    ) -> ArtifactSnapshot:
        async with self._batch_locks.batch_lock(
            batch_id,
            marker_registry=self._marker_registry,
        ):
            guard = await self._repository.acquire_content_write(
                batch_id,
                result.file_task_id,
                now=self._now_factory(),
                lease_seconds=self._write_lease_seconds,
            )
            bundle: ArtifactBundle | None = None
            registered = False
            try:
                cancellation.checkpoint()
                await progress.report(ProcessingStage.PACKAGING, ProgressCounters(0, None, ProgressUnit.BYTES))
                bundle = self._bundler.publish(
                    result,
                    publication,
                    artifact_root=artifact_root,
                    batch_id=batch_id,
                    created_at=created_at,
                    expires_at=expires_at,
                )
                cancellation.checkpoint()
                await progress.report(
                    ProcessingStage.PACKAGING,
                    ProgressCounters(bundle.size_bytes, bundle.size_bytes, ProgressUnit.BYTES),
                )
                await progress.report(ProcessingStage.PUBLISHING, ProgressCounters(0, 1, ProgressUnit.ITEMS))
                snapshot = await self._repository.register(
                    bundle,
                    publication.records,
                    write_guard=guard,
                )
                registered = True
                cancellation.checkpoint()
                await progress.report(ProcessingStage.PUBLISHING, ProgressCounters(1, 1, ProgressUnit.ITEMS))
                return snapshot
            except BaseException:
                if bundle is not None and not registered:
                    self._bundler.scrub_published(bundle)
                raise
            finally:
                await self._repository.release_content_write(guard)
