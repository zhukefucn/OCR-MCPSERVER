"""Collect and safely validate structured MinerU image candidates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import stat
import unicodedata
import warnings

from PIL import Image, ImageSequence, UnidentifiedImageError

from ..domain import (
    CandidateCollection,
    CandidateCollectionErrorCode,
    CandidateCollectionFailure,
    CandidateReference,
    CandidateSourceKind,
    ImageCandidate,
    MinerUDocumentResult,
    MinerUImageFormat,
    SecondaryOCREngine,
    SecondaryProcessingRecord,
    StoredFile,
    SupportedMediaType,
)


_PILLOW_FORMATS = {
    "PNG": MinerUImageFormat.PNG,
    "JPEG": MinerUImageFormat.JPEG,
    "JPEG2000": MinerUImageFormat.JPEG2000,
    "WEBP": MinerUImageFormat.WEBP,
    "GIF": MinerUImageFormat.GIF,
    "BMP": MinerUImageFormat.BMP,
    "TIFF": MinerUImageFormat.TIFF,
}
_FORMAT_EXTENSIONS = {
    MinerUImageFormat.PNG: frozenset({".png"}),
    MinerUImageFormat.JPEG: frozenset({".jpg", ".jpeg"}),
    MinerUImageFormat.JPEG2000: frozenset({".jp2", ".jpeg2000"}),
    MinerUImageFormat.WEBP: frozenset({".webp"}),
    MinerUImageFormat.GIF: frozenset({".gif"}),
    MinerUImageFormat.BMP: frozenset({".bmp"}),
    MinerUImageFormat.TIFF: frozenset({".tif", ".tiff"}),
}
_WINDOWS_DEVICES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(frozen=True, slots=True)
class _InspectedImage:
    path: Path
    sha256: str
    size_bytes: int
    image_format: MinerUImageFormat
    width: int
    height: int


@dataclass(slots=True)
class _CandidateBuilder:
    inspected: _InspectedImage
    aliases: list[Path]
    references: list[CandidateReference]


@dataclass(slots=True)
class _OpenedCandidate:
    descriptor: int
    path: Path
    initial_name_stat: os.stat_result
    parent_descriptor: int | None = None
    final_name: str | None = None

    def current_name_stat(self) -> os.stat_result:
        if self.parent_descriptor is not None and self.final_name is not None:
            return os.stat(
                self.final_name,
                dir_fd=self.parent_descriptor,
                follow_symlinks=False,
            )
        return os.lstat(self.path)


def collect_image_candidates(
    result: MinerUDocumentResult,
    *,
    result_version: int,
    engine: SecondaryOCREngine,
    standalone_image: StoredFile | None = None,
    max_image_pixels: int,
) -> CandidateCollection:
    """Return one deterministic pending record per unique candidate image."""

    if (
        not isinstance(result_version, int)
        or isinstance(result_version, bool)
        or result_version < 1
        or not isinstance(max_image_pixels, int)
        or isinstance(max_image_pixels, bool)
        or max_image_pixels < 1
        or not isinstance(engine, SecondaryOCREngine)
    ):
        raise CandidateCollectionFailure(
            CandidateCollectionErrorCode.INVARIANT_VIOLATION
        ) from None

    manifest = _read_manifest(result.content_list_v2_path)
    entries = _manifest_entries(manifest)
    path_cache: dict[str, _InspectedImage] = {}
    normalized_paths: dict[tuple[str, ...], str] = {}
    builders: dict[str, _CandidateBuilder] = {}
    ordered_hashes: list[str] = []

    for manifest_path, reference in entries:
        normalized_key = _normalized_path_key(manifest_path)
        previous_path = normalized_paths.get(normalized_key)
        if previous_path is not None and previous_path != manifest_path:
            _fail(CandidateCollectionErrorCode.UNSAFE_OR_MISSING_PATH)
        normalized_paths[normalized_key] = manifest_path
        inspected = path_cache.get(manifest_path)
        if inspected is None:
            candidate_path = _resolve_manifest_path(result, manifest_path)
            inspected = _inspect_image(
                candidate_path,
                expected_extension=candidate_path.suffix,
                max_image_pixels=max_image_pixels,
                confined_root=result.images_directory,
            )
            path_cache[manifest_path] = inspected
        _merge_candidate(builders, ordered_hashes, inspected, reference)

    if standalone_image is not None:
        _validate_standalone_contract(standalone_image)
        inspected = _inspect_image(
            standalone_image.path,
            expected_extension=standalone_image.extension,
            max_image_pixels=max_image_pixels,
            confined_root=None,
        )
        if (
            inspected.sha256 != standalone_image.sha256
            or inspected.size_bytes != standalone_image.size_bytes
            or inspected.width != standalone_image.width
            or inspected.height != standalone_image.height
        ):
            _fail(CandidateCollectionErrorCode.CHANGED_DURING_INSPECTION)
        _merge_candidate(
            builders,
            ordered_hashes,
            inspected,
            CandidateReference.standalone_input(),
        )

    candidates = tuple(
        _freeze_candidate(
            builders[content_hash],
            file_task_id=result.file_task_id,
            result_version=result_version,
        )
        for content_hash in ordered_hashes
    )
    records = tuple(
        SecondaryProcessingRecord.pending_for(candidate, engine=engine)
        for candidate in candidates
    )
    return CandidateCollection(
        file_task_id=result.file_task_id,
        result_version=result_version,
        candidates=candidates,
        processing_records=records,
    )


def _read_manifest(path: Path) -> object:
    failed = False
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except CandidateCollectionFailure:
        raise
    except Exception:
        failed = True
        manifest = None
    if failed:
        _fail(CandidateCollectionErrorCode.INVALID_MANIFEST)
    return manifest


def _manifest_entries(
    manifest: object,
) -> tuple[tuple[str, CandidateReference], ...]:
    if not isinstance(manifest, list):
        _fail(CandidateCollectionErrorCode.INVALID_MANIFEST)
    entries: list[tuple[str, CandidateReference]] = []
    for page_index, page in enumerate(manifest):
        if not isinstance(page, list):
            _fail(CandidateCollectionErrorCode.INVALID_MANIFEST)
        for node_index, node in enumerate(page):
            if not isinstance(node, Mapping):
                _fail(CandidateCollectionErrorCode.INVALID_MANIFEST)
            if "content" not in node:
                continue
            content = node["content"]
            if not isinstance(content, Mapping):
                _fail(CandidateCollectionErrorCode.INVALID_MANIFEST)
            if "image_source" not in content:
                continue
            image_source = content["image_source"]
            if not isinstance(image_source, Mapping):
                _fail(CandidateCollectionErrorCode.INVALID_MANIFEST)
            path = image_source.get("path")
            if not isinstance(path, str) or not path:
                _fail(CandidateCollectionErrorCode.INVALID_MANIFEST)
            node_type = node.get("type")
            if not isinstance(node_type, str) or not node_type:
                _fail(CandidateCollectionErrorCode.INVALID_MANIFEST)
            bbox = _validated_bbox(node.get("bbox")) if "bbox" in node else None
            try:
                reference = CandidateReference(
                    source_kind=CandidateSourceKind.MINERU_NODE,
                    page_index=page_index,
                    node_index=node_index,
                    json_pointer=f"/{page_index}/{node_index}",
                    original_node_type=node_type,
                    bbox=bbox,
                )
            except (TypeError, ValueError) as exc:
                raise CandidateCollectionFailure(
                    CandidateCollectionErrorCode.INVALID_MANIFEST, cause=exc
                ) from None
            entries.append((path, reference))
    return tuple(entries)


def _validated_bbox(value: object) -> tuple[float, float, float, float]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(not _is_finite_number(item) for item in value)
    ):
        _fail(CandidateCollectionErrorCode.INVALID_MANIFEST)
    x0, y0, x1, y1 = value
    if not (0 <= x0 <= x1 <= 1000 and 0 <= y0 <= y1 <= 1000):
        _fail(CandidateCollectionErrorCode.INVALID_MANIFEST)
    return (x0, y0, x1, y1)


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _safe_posix_parts(raw_path: str) -> tuple[str, ...]:
    if (
        not raw_path
        or raw_path.startswith("/")
        or raw_path.startswith("//")
        or "\\" in raw_path
        or re.match(r"^[A-Za-z]:($|/)", raw_path)
    ):
        _fail(CandidateCollectionErrorCode.UNSAFE_OR_MISSING_PATH)
    parts = tuple(raw_path.split("/"))
    for part in parts:
        if (
            not part
            or part in {".", ".."}
            or ":" in part
            or part != part.rstrip(" .")
            or any(
                unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                for character in part
            )
            or part.split(".", 1)[0].upper() in _WINDOWS_DEVICES
        ):
            _fail(CandidateCollectionErrorCode.UNSAFE_OR_MISSING_PATH)
    return parts


def _normalized_path_key(raw_path: str) -> tuple[str, ...]:
    return tuple(
        unicodedata.normalize("NFC", part).casefold().rstrip(" .")
        for part in _safe_posix_parts(raw_path)
    )


def _resolve_manifest_path(result: MinerUDocumentResult, raw_path: str) -> Path:
    parts = _safe_posix_parts(raw_path)
    candidate = result.content_list_v2_path.parent.joinpath(*parts).absolute()
    root = result.images_directory.absolute()
    try:
        if not candidate.is_relative_to(root):
            _fail(CandidateCollectionErrorCode.UNSAFE_OR_MISSING_PATH)
    except (OSError, ValueError) as exc:
        raise CandidateCollectionFailure(
            CandidateCollectionErrorCode.UNSAFE_OR_MISSING_PATH, cause=exc
        ) from None
    return candidate


def _open_candidate(
    path: Path, *, confined_root: Path | None
) -> _OpenedCandidate:
    opened: _OpenedCandidate | None = None
    descriptor = -1
    failed = False
    try:
        if confined_root is not None and os.name != "nt":
            opened = _open_posix_confined(path, confined_root)
        else:
            descriptor = (
                _windows_open_no_reparse(path)
                if os.name == "nt"
                else os.open(
                    path,
                    os.O_RDONLY
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
            )
            if confined_root is not None:
                _assert_windows_descriptor_path(descriptor, path, confined_root)
            opened = _OpenedCandidate(
                descriptor=descriptor,
                path=path,
                initial_name_stat=os.lstat(path),
            )
    except (OSError, RuntimeError, ValueError):
        failed = True
    if failed or opened is None:
        if opened is not None:
            _close_descriptor_ignoring_errors(opened.descriptor)
            if opened.parent_descriptor is not None:
                _close_descriptor_ignoring_errors(opened.parent_descriptor)
        elif descriptor >= 0:
            _close_descriptor_ignoring_errors(descriptor)
        _fail(CandidateCollectionErrorCode.UNSAFE_OR_MISSING_PATH)
    return opened


def _open_posix_confined(path: Path, root: Path) -> _OpenedCandidate:
    absolute_root = root.absolute()
    candidate = path.absolute()
    if not candidate.is_relative_to(absolute_root):
        raise OSError("outside root")
    relative = candidate.relative_to(absolute_root)
    if not relative.parts:
        raise OSError("candidate is root")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    current = os.open(Path(absolute_root.anchor), directory_flags)
    try:
        if not stat.S_ISDIR(os.fstat(current).st_mode):
            raise OSError("filesystem anchor is not a directory")
        directory_parts = (*absolute_root.parts[1:], *relative.parts[:-1])
        for part in directory_parts:
            child = os.open(part, directory_flags, dir_fd=current)
            try:
                if not stat.S_ISDIR(os.fstat(child).st_mode):
                    raise OSError("component is not a directory")
            except BaseException:
                _close_descriptor_ignoring_errors(child)
                raise
            current = _transfer_directory_ownership(current, child)
        final_name = relative.parts[-1]
        file_flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(final_name, file_flags, dir_fd=current)
        try:
            initial_name_stat = os.stat(
                final_name, dir_fd=current, follow_symlinks=False
            )
        except BaseException:
            _close_descriptor_ignoring_errors(descriptor)
            raise
        return _OpenedCandidate(
            descriptor=descriptor,
            path=path,
            initial_name_stat=initial_name_stat,
            parent_descriptor=current,
            final_name=final_name,
        )
    except BaseException:
        _close_descriptor_ignoring_errors(current)
        raise


def _transfer_directory_ownership(current: int, child: int) -> int:
    try:
        os.close(current)
    except OSError:
        _close_descriptor_ignoring_errors(child)
        _close_descriptor_ignoring_errors(current)
        raise
    return child


def _windows_open_no_reparse(path: Path) -> int:
    import ctypes
    from ctypes import wintypes

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
        0x80000000,
        0x1 | 0x2 | 0x4,
        None,
        3,
        0x00200000,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    class _AttributeTagInfo(ctypes.Structure):
        _fields_ = (
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        )

    attribute_info = _AttributeTagInfo()
    get_attributes = kernel32.GetFileInformationByHandleEx
    get_attributes.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_attributes.restype = wintypes.BOOL
    if not get_attributes(
        handle,
        9,
        ctypes.byref(attribute_info),
        ctypes.sizeof(attribute_info),
    ) or attribute_info.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        _windows_close_handle(int(handle))
        raise OSError("invalid reparse target")
    return _windows_handle_to_descriptor(int(handle))


def _windows_handle_to_descriptor(handle: int) -> int:
    import msvcrt

    try:
        return msvcrt.open_osfhandle(
            handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except BaseException:
        _windows_close_handle(handle)
        raise


def _windows_close_handle(handle: int) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle(handle)


def _assert_windows_descriptor_path(
    descriptor: int, path: Path, confined_root: Path
) -> None:
    actual = _windows_final_path(descriptor)
    expected = _windows_long_path(path)
    root = _windows_final_directory_path(confined_root).rstrip("\\/")
    normalized_actual = os.path.normcase(os.path.normpath(actual))
    normalized_expected = os.path.normcase(os.path.normpath(expected))
    normalized_root = os.path.normcase(os.path.normpath(root))
    if normalized_actual != normalized_expected or not normalized_actual.startswith(
        normalized_root + os.sep
    ):
        raise OSError("descriptor escaped root")


def _windows_final_directory_path(path: Path) -> str:
    import ctypes
    from ctypes import wintypes

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
        0x80 | 0x0001,
        0x1 | 0x2 | 0x4,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if _windows_handle_is_reparse(int(handle)):
            raise OSError("invalid reparse root")
        return _windows_final_path_from_handle(int(handle))
    finally:
        kernel32.CloseHandle(handle)


def _windows_long_path(path: Path) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_long_path = kernel32.GetLongPathNameW
    get_long_path.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    )
    get_long_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_long_path(str(path.absolute()), buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.value


def _windows_handle_is_reparse(handle: int) -> bool:
    import ctypes
    from ctypes import wintypes

    class _AttributeTagInfo(ctypes.Structure):
        _fields_ = (
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_attributes = kernel32.GetFileInformationByHandleEx
    get_attributes.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_attributes.restype = wintypes.BOOL
    attribute_info = _AttributeTagInfo()
    if not get_attributes(
        handle,
        9,
        ctypes.byref(attribute_info),
        ctypes.sizeof(attribute_info),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return bool(attribute_info.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _windows_final_path(descriptor: int) -> str:
    import msvcrt

    return _windows_final_path_from_handle(msvcrt.get_osfhandle(descriptor))


def _windows_final_path_from_handle(handle: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_final_path(handle, buffer, len(buffer), 0)
    if length == 0 or length >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _close_descriptor_ignoring_errors(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _inspect_image(
    path: Path,
    *,
    expected_extension: str,
    max_image_pixels: int,
    confined_root: Path | None,
) -> _InspectedImage:
    opened = _open_candidate(path, confined_root=confined_root)
    descriptor_fault = False
    processing_failure: CandidateCollectionFailure | None = None
    stream = None
    digest = sha256()
    byte_count = 0
    image_format: MinerUImageFormat | None = None
    width = height = 0
    opened_stat = after_fd_stat = after_path_stat = opened.initial_name_stat
    try:
        try:
            opened_stat = os.fstat(opened.descriptor)
            if not stat.S_ISREG(opened_stat.st_mode) or _is_reparse(opened_stat):
                _fail(CandidateCollectionErrorCode.UNSAFE_OR_MISSING_PATH)
            if not _same_file_identity(opened.initial_name_stat, opened_stat):
                _fail(CandidateCollectionErrorCode.CHANGED_DURING_INSPECTION)
            stream = os.fdopen(opened.descriptor, "rb", closefd=False)
            while chunk := stream.read(64 * 1024):
                digest.update(chunk)
                byte_count += len(chunk)
            if byte_count < 1:
                _fail(CandidateCollectionErrorCode.INVALID_IMAGE)
            stream.seek(0)
            image_format, width, height = _decode_image(
                stream,
                expected_extension=expected_extension,
                max_image_pixels=max_image_pixels,
            )
            after_fd_stat = os.fstat(opened.descriptor)
            after_path_stat = opened.current_name_stat()
        except CandidateCollectionFailure as exc:
            processing_failure = exc
        except (OSError, ValueError):
            descriptor_fault = True
    finally:
        if stream is not None:
            try:
                stream.close()
            except OSError:
                descriptor_fault = True
        try:
            os.close(opened.descriptor)
        except OSError:
            descriptor_fault = True
        if opened.parent_descriptor is not None:
            try:
                os.close(opened.parent_descriptor)
            except OSError:
                descriptor_fault = True

    if processing_failure is not None:
        raise processing_failure
    if descriptor_fault:
        _fail(CandidateCollectionErrorCode.CHANGED_DURING_INSPECTION)
    if (
        image_format is None
        or byte_count != opened_stat.st_size
        or not _same_file_identity(opened_stat, after_fd_stat)
        or not _same_file_identity(opened.initial_name_stat, after_path_stat)
    ):
        _fail(CandidateCollectionErrorCode.CHANGED_DURING_INSPECTION)
    return _InspectedImage(
        path=path,
        sha256=digest.hexdigest(),
        size_bytes=byte_count,
        image_format=image_format,
        width=width,
        height=height,
    )


def _decode_image(
    stream,
    *,
    expected_extension: str,
    max_image_pixels: int,
) -> tuple[MinerUImageFormat, int, int]:
    decode_failed = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(stream) as image:
                image_format = _PILLOW_FORMATS.get(image.format or "")
                if image_format is None:
                    _fail(CandidateCollectionErrorCode.INVALID_IMAGE)
                width, height = image.size
                _validate_dimensions(width, height, max_image_pixels)
                if expected_extension.lower() not in _FORMAT_EXTENSIONS[image_format]:
                    _fail(CandidateCollectionErrorCode.INVALID_IMAGE)
                image.verify()
            stream.seek(0)
            with Image.open(stream) as image:
                if _PILLOW_FORMATS.get(image.format or "") is not image_format:
                    _fail(CandidateCollectionErrorCode.CHANGED_DURING_INSPECTION)
                if image.size != (width, height):
                    _fail(CandidateCollectionErrorCode.CHANGED_DURING_INSPECTION)
                _validate_dimensions(*image.size, max_image_pixels)
                aggregate_pixels = 0
                for frame in ImageSequence.Iterator(image):
                    _validate_dimensions(*frame.size, max_image_pixels)
                    aggregate_pixels += frame.width * frame.height
                    if aggregate_pixels > max_image_pixels:
                        _fail(CandidateCollectionErrorCode.INVALID_IMAGE)
                    frame.load()
    except CandidateCollectionFailure:
        raise
    except Exception:
        decode_failed = True
    if decode_failed:
        _fail(CandidateCollectionErrorCode.INVALID_IMAGE)
    return image_format, width, height


def _validate_dimensions(width: int, height: int, max_image_pixels: int) -> None:
    if width < 1 or height < 1 or width * height > max_image_pixels:
        _fail(CandidateCollectionErrorCode.INVALID_IMAGE)


def _is_reparse(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
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


def _validate_standalone_contract(stored: StoredFile) -> None:
    if (
        stored.media_type not in {SupportedMediaType.PNG, SupportedMediaType.JPEG}
        or stored.extension.lower() not in {".png", ".jpg", ".jpeg"}
        or stored.width is None
        or stored.height is None
        or stored.width < 1
        or stored.height < 1
        or stored.size_bytes < 1
        or len(stored.sha256) != 64
    ):
        _fail(CandidateCollectionErrorCode.INVALID_IMAGE)
    if stored.media_type is SupportedMediaType.PNG and stored.extension.lower() != ".png":
        _fail(CandidateCollectionErrorCode.INVALID_IMAGE)
    if stored.media_type is SupportedMediaType.JPEG and stored.extension.lower() not in {
        ".jpg",
        ".jpeg",
    }:
        _fail(CandidateCollectionErrorCode.INVALID_IMAGE)


def _merge_candidate(
    builders: dict[str, _CandidateBuilder],
    ordered_hashes: list[str],
    inspected: _InspectedImage,
    reference: CandidateReference,
) -> None:
    builder = builders.get(inspected.sha256)
    if builder is None:
        builders[inspected.sha256] = _CandidateBuilder(
            inspected=inspected,
            aliases=[inspected.path],
            references=[reference],
        )
        ordered_hashes.append(inspected.sha256)
        return
    if inspected.path not in builder.aliases:
        builder.aliases.append(inspected.path)
    builder.references.append(reference)


def _freeze_candidate(
    builder: _CandidateBuilder,
    *,
    file_task_id: str,
    result_version: int,
) -> ImageCandidate:
    identity = sha256(
        (
            "image-candidate\0"
            f"{file_task_id}\0{result_version}\0{builder.inspected.sha256}"
        ).encode("utf-8")
    ).hexdigest()
    references = tuple(builder.references)
    hints = tuple(
        dict.fromkeys(
            reference.original_node_type
            for reference in references
            if reference.original_node_type is not None
        )
    )
    return ImageCandidate(
        candidate_id=f"candidate-{identity}",
        file_task_id=file_task_id,
        result_version=result_version,
        sha256=builder.inspected.sha256,
        size_bytes=builder.inspected.size_bytes,
        image_format=builder.inspected.image_format,
        width=builder.inspected.width,
        height=builder.inspected.height,
        primary_path=builder.aliases[0],
        alias_paths=tuple(builder.aliases),
        references=references,
        node_type_hints=hints,
    )


def _fail(code: CandidateCollectionErrorCode) -> None:
    raise CandidateCollectionFailure(code) from None
