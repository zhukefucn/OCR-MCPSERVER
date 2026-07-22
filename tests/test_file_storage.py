from __future__ import annotations

from collections.abc import AsyncIterator
from io import BytesIO
import os
from pathlib import Path
import subprocess
from uuid import UUID, uuid4

import pytest
from PIL import Image
from pypdf import PdfWriter

from ocr_mcp_server.domain.errors import FileIntakeFailure
from ocr_mcp_server.domain.files import IncomingFile, SupportedMediaType
from ocr_mcp_server.services.file_storage import FileStorage
from ocr_mcp_server.services.file_validation import FileValidator


def _pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 3), color=(1, 2, 3)).save(output, format="PNG")
    return output.getvalue()


async def _chunks(payload: bytes, chunk_size: int = 17) -> AsyncIterator[bytes]:
    for offset in range(0, len(payload), chunk_size):
        yield payload[offset : offset + chunk_size]


def _validator() -> FileValidator:
    return FileValidator(max_pages=500, max_image_pixels=1_000)


@pytest.mark.asyncio
async def test_storage_streams_validated_file_to_server_generated_uuid_path(
    tmp_path: Path,
) -> None:
    storage = FileStorage(tmp_path / "data")
    batch_id = str(uuid4())
    payload = _png_bytes()

    stored = await storage.store(
        batch_id,
        IncomingFile("../../private-name.PNG", "image/png", _chunks(payload)),
        max_file_size_bytes=len(payload),
        validator=_validator(),
    )

    assert stored.path.parent == (tmp_path / "data" / batch_id / "input").absolute()
    assert stored.path.name == f"{stored.file_id}.png"
    assert str(UUID(stored.file_id)) == stored.file_id
    assert "private-name" not in str(stored.path)
    assert stored.path.read_bytes() == payload
    assert stored.media_type is SupportedMediaType.PNG
    assert stored.size_bytes == len(payload)
    assert not list(stored.path.parent.glob("*.part"))


@pytest.mark.asyncio
async def test_storage_stops_at_one_byte_over_limit_and_cleans_part(
    tmp_path: Path,
) -> None:
    payload = _pdf_bytes()
    consumed = 0

    async def observed_chunks() -> AsyncIterator[bytes]:
        nonlocal consumed
        for chunk in (payload, b"x", b"must-not-be-consumed"):
            consumed += 1
            yield chunk

    storage = FileStorage(tmp_path / "data")
    batch_id = str(uuid4())
    with pytest.raises(FileIntakeFailure) as exc_info:
        await storage.store(
            batch_id,
            IncomingFile("document.pdf", "application/pdf", observed_chunks()),
            max_file_size_bytes=len(payload),
            validator=_validator(),
        )

    assert exc_info.value.code == "file_too_large"
    assert consumed == 2
    input_dir = tmp_path / "data" / batch_id / "input"
    assert not list(input_dir.glob("*.part"))
    assert not list(input_dir.glob("*.pdf"))


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_id", ["../escape", "not-a-uuid", "A" * 36])
async def test_storage_rejects_noncanonical_batch_paths(
    tmp_path: Path, batch_id: str
) -> None:
    storage = FileStorage(tmp_path / "data")

    with pytest.raises(FileIntakeFailure) as exc_info:
        await storage.store(
            batch_id,
            IncomingFile("document.pdf", "application/pdf", _chunks(_pdf_bytes())),
            max_file_size_bytes=10_000,
            validator=_validator(),
        )

    assert exc_info.value.code == "path_unsafe"
    assert not (tmp_path / "escape").exists()


@pytest.mark.asyncio
async def test_storage_rejects_preexisting_symlink_in_storage_chain(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    batch_id = str(uuid4())
    batch_dir = data_root / batch_id
    batch_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, batch_dir / "input", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {type(exc).__name__}")

    with pytest.raises(FileIntakeFailure) as exc_info:
        await FileStorage(data_root).store(
            batch_id,
            IncomingFile("document.pdf", "application/pdf", _chunks(_pdf_bytes())),
            max_file_size_bytes=10_000,
            validator=_validator(),
        )

    assert exc_info.value.code == "path_unsafe"
    assert not list(outside.iterdir())


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
async def test_storage_rejects_windows_junction_in_storage_chain(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    batch_id = str(uuid4())
    batch_dir = data_root / batch_id
    batch_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = batch_dir / "input"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("directory junctions unavailable")

    try:
        with pytest.raises(FileIntakeFailure) as exc_info:
            await FileStorage(data_root).store(
                batch_id,
                IncomingFile(
                    "document.pdf", "application/pdf", _chunks(_pdf_bytes())
                ),
                max_file_size_bytes=10_000,
                validator=_validator(),
            )
        assert exc_info.value.code == "path_unsafe"
        assert not list(outside.iterdir())
    finally:
        if os.path.lexists(junction):
            os.rmdir(junction)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX open directory handle regression")
async def test_storage_rejects_input_directory_replaced_by_symlink_mid_stream(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    batch_id = str(uuid4())
    input_dir = data_root / batch_id / "input"
    moved_dir = data_root / batch_id / "input-held"
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = _pdf_bytes()

    async def replaced_directory() -> AsyncIterator[bytes]:
        yield payload[: len(payload) // 2]
        part = next(input_dir.glob("*.part"))
        input_dir.rename(moved_dir)
        os.symlink(outside, input_dir, target_is_directory=True)
        (outside / part.name).write_bytes(payload)
        yield payload[len(payload) // 2 :]

    try:
        with pytest.raises(FileIntakeFailure) as exc_info:
            await FileStorage(data_root).store(
                batch_id,
                IncomingFile(
                    "document.pdf", "application/pdf", replaced_directory()
                ),
                max_file_size_bytes=10_000,
                validator=_validator(),
            )
        assert exc_info.value.code == "path_unsafe"
        assert not list(outside.glob("*.pdf"))
    finally:
        if input_dir.is_symlink():
            input_dir.unlink()
        if moved_dir.exists() and not input_dir.exists():
            moved_dir.rename(input_dir)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX staged inode replacement regression")
async def test_storage_never_validates_or_publishes_replaced_staged_name(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    batch_id = str(uuid4())
    input_dir = data_root / batch_id / "input"
    payload = _pdf_bytes()
    replacement_writer = PdfWriter()
    replacement_writer.add_blank_page(width=72, height=72)
    replacement_writer.add_blank_page(width=72, height=72)
    replacement_output = BytesIO()
    replacement_writer.write(replacement_output)
    replacement = replacement_output.getvalue()

    async def replaced_stage() -> AsyncIterator[bytes]:
        yield payload[: len(payload) // 2]
        part = next(input_dir.glob("*.part"))
        part.unlink()
        part.write_bytes(replacement)
        yield payload[len(payload) // 2 :]

    try:
        stored = await FileStorage(data_root).store(
            batch_id,
            IncomingFile("document.pdf", "application/pdf", replaced_stage()),
            max_file_size_bytes=10_000,
            validator=_validator(),
        )
    except FileIntakeFailure as failure:
        assert failure.code == "path_unsafe"
        assert not list(input_dir.glob("*.pdf"))
    else:
        assert stored.path.read_bytes() == payload
        assert stored.page_count == 1


@pytest.mark.asyncio
async def test_publication_cleanup_fault_never_escapes_raw_oserror_or_leaves_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_unlink = Path.unlink
    failed_once = False

    def fail_first_part_unlink(path: Path, *args, **kwargs):
        nonlocal failed_once
        if path.suffix == ".part" and not failed_once:
            failed_once = True
            raise OSError("sensitive cleanup filesystem detail")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_first_part_unlink)
    data_root = tmp_path / "data"
    batch_id = str(uuid4())
    try:
        stored = await FileStorage(data_root).store(
            batch_id,
            IncomingFile("document.pdf", "application/pdf", _chunks(_pdf_bytes())),
            max_file_size_bytes=10_000,
            validator=_validator(),
        )
    except FileIntakeFailure as failure:
        assert failure.code == "path_unsafe"
        assert failure.__context__ is None
    else:
        assert stored.path.exists()

    input_dir = data_root / batch_id / "input"
    assert not list(input_dir.glob("*.part"))


@pytest.mark.asyncio
async def test_storage_never_overwrites_an_existing_target(tmp_path: Path) -> None:
    file_id = "cc13cd13-86e2-4659-8677-959e494f7091"
    batch_id = str(uuid4())
    input_dir = tmp_path / "data" / batch_id / "input"
    input_dir.mkdir(parents=True)
    target = input_dir / f"{file_id}.pdf"
    target.write_bytes(b"existing")
    storage = FileStorage(tmp_path / "data", id_factory=lambda: file_id)

    with pytest.raises(FileIntakeFailure) as exc_info:
        await storage.store(
            batch_id,
            IncomingFile("document.pdf", "application/pdf", _chunks(_pdf_bytes())),
            max_file_size_bytes=10_000,
            validator=_validator(),
        )

    assert exc_info.value.code == "path_unsafe"
    assert target.read_bytes() == b"existing"
    assert not list(input_dir.glob("*.part"))


def test_batch_usage_counts_only_direct_server_files_without_following_links(
    tmp_path: Path,
) -> None:
    batch_id = str(uuid4())
    input_dir = tmp_path / "data" / batch_id / "input"
    input_dir.mkdir(parents=True)
    (input_dir / f"{uuid4()}.pdf").write_bytes(b"123")
    (input_dir / f"{uuid4()}.png").write_bytes(b"12345")
    (input_dir / "ignored.part").write_bytes(b"1234567")
    nested = input_dir / "nested"
    nested.mkdir()
    (nested / f"{uuid4()}.pdf").write_bytes(b"not-counted")

    usage = FileStorage(tmp_path / "data").batch_usage(batch_id)

    assert usage.file_count == 2
    assert usage.total_bytes == 8


def test_batch_usage_is_read_only_for_an_absent_batch(tmp_path: Path) -> None:
    data_root = tmp_path / "data"

    usage = FileStorage(data_root).batch_usage(str(uuid4()))

    assert (usage.file_count, usage.total_bytes) == (0, 0)
    assert not data_root.exists()


def test_batch_usage_oserror_has_no_sensitive_exception_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    batch_id = str(uuid4())
    (data_root / batch_id / "input").mkdir(parents=True)

    def failed_scan(path):
        raise OSError("sensitive directory scan detail")

    monkeypatch.setattr(os, "scandir", failed_scan)
    with pytest.raises(FileIntakeFailure) as exc_info:
        FileStorage(data_root).batch_usage(batch_id)

    assert exc_info.value.code == "path_unsafe"
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_public_store_sanitizes_atomic_publish_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_publish(storage, directory, source_name, target_name):
        raise OSError("sensitive atomic publish detail")

    monkeypatch.setattr(FileStorage, "_publish_no_replace", failed_publish)
    data_root = tmp_path / "data"
    batch_id = str(uuid4())
    with pytest.raises(FileIntakeFailure) as exc_info:
        await FileStorage(data_root).store(
            batch_id,
            IncomingFile("document.pdf", "application/pdf", _chunks(_pdf_bytes())),
            max_file_size_bytes=10_000,
            validator=_validator(),
        )

    assert exc_info.value.code == "path_unsafe"
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert not list((data_root / batch_id / "input").glob("*.part"))


def test_hardlink_fallback_retries_transient_source_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FileStorage(tmp_path)
    directory = storage._open_directory(tmp_path)
    source_name = "source.part"
    target_name = "target.pdf"
    (tmp_path / source_name).write_bytes(b"payload")
    original_unlink = storage._unlink_name
    source_failures = 0

    def transient_unlink(opened, name, *, missing_ok=False):
        nonlocal source_failures
        if name == source_name and source_failures == 0:
            source_failures += 1
            raise OSError("transient cleanup failure")
        return original_unlink(opened, name, missing_ok=missing_ok)

    monkeypatch.setattr(storage, "_unlink_name", transient_unlink)
    try:
        storage._hardlink_publish(directory, source_name, target_name)
    finally:
        if directory.descriptor is not None:
            os.close(directory.descriptor)

    assert not (tmp_path / source_name).exists()
    assert (tmp_path / target_name).read_bytes() == b"payload"


def test_hardlink_fallback_rolls_back_target_when_source_unlink_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FileStorage(tmp_path)
    directory = storage._open_directory(tmp_path)
    source_name = "source.part"
    target_name = "target.pdf"
    (tmp_path / source_name).write_bytes(b"payload")
    original_unlink = storage._unlink_name

    def persistent_unlink(opened, name, *, missing_ok=False):
        if name == source_name:
            raise OSError("persistent cleanup failure")
        return original_unlink(opened, name, missing_ok=missing_ok)

    monkeypatch.setattr(storage, "_unlink_name", persistent_unlink)
    try:
        with pytest.raises(OSError):
            storage._hardlink_publish(directory, source_name, target_name)
    finally:
        if directory.descriptor is not None:
            os.close(directory.descriptor)

    assert (tmp_path / source_name).read_bytes() == b"payload"
    assert not (tmp_path / target_name).exists()
