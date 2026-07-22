from __future__ import annotations

from collections.abc import AsyncIterator
from io import BytesIO
import os
from pathlib import Path
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
