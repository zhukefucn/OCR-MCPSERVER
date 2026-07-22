"""Blocking, cooperatively cancellable MinerU ZIP archive processing."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading
import unicodedata
from zipfile import BadZipFile, ZipFile, ZipInfo

from ..domain import MinerUErrorCode, MinerUFailure


class ArchiveWorkCancelled(Exception):
    """Internal signal raised after cooperative archive-worker cancellation."""


@dataclass(frozen=True, slots=True)
class ExtractedArchive:
    """Validated document layout within a staging tree."""

    document_name: str
    parse_directory: str


def publish_directory_no_replace(source: Path, target: Path) -> None:
    """Atomically publish a staged directory without replacing any target."""

    if os.name == "nt":
        os.rename(source, target)
        return
    if sys.platform.startswith("linux"):
        _linux_rename_directory_noreplace(source, target)
        return
    raise OSError(
        errno.ENOTSUP,
        "atomic no-replace directory publication is unsupported",
    )


def _linux_rename_directory_noreplace(source: Path, target: Path) -> None:
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOTSUP, "renameat2 is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    open_flags = os.O_RDONLY
    open_flags |= getattr(os, "O_DIRECTORY", 0)
    open_flags |= getattr(os, "O_CLOEXEC", 0)
    open_flags |= getattr(os, "O_NOFOLLOW", 0)
    source_parent = os.open(source.parent, open_flags)
    try:
        target_parent = os.open(target.parent, open_flags)
        try:
            result = renameat2(
                source_parent,
                os.fsencode(source.name),
                target_parent,
                os.fsencode(target.name),
                1,
            )
            error_number = ctypes.get_errno()
        finally:
            os.close(target_parent)
    finally:
        os.close(source_parent)
    if result == 0:
        return
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number))
    unsupported = {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}
    if hasattr(errno, "EOPNOTSUPP"):
        unsupported.add(errno.EOPNOTSUPP)
    if error_number in unsupported:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace directory publication is unsupported",
        )
    raise OSError(error_number, os.strerror(error_number))


def extract_archive(
    archive_path: Path,
    extracted_root: Path,
    *,
    expected_stem: str,
    max_entries: int,
    max_uncompressed_bytes: int,
    cancel_event: threading.Event,
) -> ExtractedArchive:
    """Validate and extract a MinerU ZIP, checking cancellation while working."""

    _check_cancelled(cancel_event)
    extracted_root.mkdir()
    try:
        with ZipFile(archive_path) as archive:
            entries, document_name, parse_directory = _validate_entries(
                archive,
                extracted_root,
                expected_stem,
                max_entries=max_entries,
                max_uncompressed_bytes=max_uncompressed_bytes,
                cancel_event=cancel_event,
            )
            actual_total = 0
            for info, parts in entries:
                _check_cancelled(cancel_event)
                destination = extracted_root.joinpath(*parts)
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    if not destination.is_dir():
                        raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("xb") as output:
                    while True:
                        _check_cancelled(cancel_event)
                        chunk = source.read(64 * 1024)
                        _check_cancelled(cancel_event)
                        if not chunk:
                            break
                        actual_total += len(chunk)
                        if actual_total > max_uncompressed_bytes:
                            raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None
                        output.write(chunk)
    except (ArchiveWorkCancelled, MinerUFailure):
        raise
    except (BadZipFile, OSError, RuntimeError, UnicodeError) as exc:
        raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE, cause=exc) from None

    _check_cancelled(cancel_event)
    manifest = (
        extracted_root
        / document_name
        / parse_directory
        / f"{expected_stem}_content_list_v2.json"
    )
    try:
        parsed_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE, cause=exc) from None
    _check_cancelled(cancel_event)
    if not isinstance(parsed_manifest, list):
        raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None
    return ExtractedArchive(document_name, parse_directory)


def _validate_entries(
    archive: ZipFile,
    extracted_root: Path,
    expected_stem: str,
    *,
    max_entries: int,
    max_uncompressed_bytes: int,
    cancel_event: threading.Event,
) -> tuple[list[tuple[ZipInfo, tuple[str, ...]]], str, str]:
    infos = archive.infolist()
    if not infos or len(infos) > max_entries:
        raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None

    validated: list[tuple[ZipInfo, tuple[str, ...]]] = []
    normalized_destinations: set[tuple[str, ...]] = set()
    declared_total = 0
    file_paths: list[tuple[str, ...]] = []
    directory_paths: list[tuple[str, ...]] = []
    manifest_paths: list[tuple[str, ...]] = []
    root = extracted_root.resolve()
    for info in infos:
        _check_cancelled(cancel_event)
        parts = _safe_zip_parts(info.orig_filename, is_directory=info.is_dir())
        normalized = tuple(
            unicodedata.normalize("NFC", part).casefold().rstrip(" .")
            for part in parts
        )
        if any(not part for part in normalized):
            raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None
        if normalized in normalized_destinations:
            raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None
        normalized_destinations.add(normalized)

        file_type = stat.S_IFMT(info.external_attr >> 16)
        accepted_types = {0, stat.S_IFDIR if info.is_dir() else stat.S_IFREG}
        if file_type not in accepted_types:
            raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None
        destination = extracted_root.joinpath(*parts).resolve()
        if not destination.is_relative_to(root):
            raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None
        if not info.is_dir():
            declared_total += info.file_size
            if declared_total > max_uncompressed_bytes:
                raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None
            file_paths.append(parts)
            if parts[-1].endswith("_content_list_v2.json"):
                manifest_paths.append(parts)
        else:
            directory_paths.append(parts)
        validated.append((info, parts))

    if not file_paths or any(len(parts) < 3 for parts in file_paths):
        raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None
    documents = {parts[0] for parts in file_paths}
    parse_directories = {parts[1] for parts in file_paths}
    if documents != {expected_stem} or len(parse_directories) != 1:
        raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None
    parse_directory = next(iter(parse_directories))
    for parts in directory_paths:
        _check_cancelled(cancel_event)
        if parts in {(expected_stem,), (expected_stem, parse_directory)}:
            continue
        if (
            len(parts) >= 3
            and parts[:2] == (expected_stem, parse_directory)
            and parts[2].casefold() == "images"
        ):
            continue
        raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None
    for parts in file_paths:
        _check_cancelled(cancel_event)
        if len(parts) > 3 and parts[2].casefold() != "images":
            raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None
        image_indexes = [
            index
            for index, part in enumerate(parts[2:], start=2)
            if part.casefold() == "images"
        ]
        if image_indexes and image_indexes != [2]:
            raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None
    expected_manifest = (
        expected_stem,
        parse_directory,
        f"{expected_stem}_content_list_v2.json",
    )
    if manifest_paths != [expected_manifest]:
        raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None
    return validated, expected_stem, parse_directory


def _safe_zip_parts(name: str, *, is_directory: bool) -> tuple[str, ...]:
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or name.startswith("/")
        or re.match(r"^[A-Za-z]:($|/)", name)
    ):
        raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None
    selected = name[:-1] if is_directory and name.endswith("/") else name
    parts = tuple(selected.split("/"))
    windows_devices = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    for part in parts:
        if (
            not part
            or part in {".", ".."}
            or ":" in part
            or any(ord(character) < 32 for character in part)
            or part.rstrip(" .").split(".", 1)[0].upper() in windows_devices
        ):
            raise MinerUFailure(MinerUErrorCode.UNSAFE_ARCHIVE) from None
    return parts


def _check_cancelled(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise ArchiveWorkCancelled
