from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import hashlib
from io import BytesIO
import multiprocessing
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest
from PIL import Image
from pypdf import PdfWriter

from ocr_mcp_server.domain.errors import FileIntakeFailure
from ocr_mcp_server.domain.files import IncomingFile
from ocr_mcp_server.services.file_intake import (
    FileIntakeService,
    validate_batch_capacity,
)
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
    Image.new("RGB", (4, 5), color=(4, 5, 6)).save(output, format="PNG")
    return output.getvalue()


async def _chunks(payload: bytes) -> AsyncIterator[bytes]:
    midpoint = max(1, len(payload) // 2)
    yield payload[:midpoint]
    yield payload[midpoint:]


def _service(
    data_root: Path,
    *,
    max_files: int = 20,
    max_file_size_bytes: int = 30 * 1024 * 1024,
    max_batch_size_bytes: int = 1024**3,
) -> FileIntakeService:
    return FileIntakeService(
        storage=FileStorage(data_root),
        validator=FileValidator(max_pages=500, max_image_pixels=1_000),
        max_files=max_files,
        max_file_size_bytes=max_file_size_bytes,
        max_batch_size_bytes=max_batch_size_bytes,
    )


def _subprocess_ingest(
    data_root: str,
    batch_id: str,
    payload: bytes,
    display_name: str,
    start_event,
    ready_queue,
    result_queue,
) -> None:
    async def content() -> AsyncIterator[bytes]:
        yield payload

    ready_queue.put("ready")
    start_event.wait(timeout=10)
    try:
        stored = asyncio.run(
            _service(Path(data_root), max_files=1).ingest_upload(
                batch_id,
                IncomingFile(display_name, "application/pdf", content()),
            )
        )
    except FileIntakeFailure as failure:
        result_queue.put(("error", failure.code))
    except BaseException as failure:
        result_queue.put(("unexpected", type(failure).__name__))
    else:
        result_queue.put(("stored", stored.file_id))


@pytest.mark.parametrize(
    ("existing_files", "existing_bytes", "incoming", "should_pass"),
    [
        (19, 0, [0], True),
        (20, 0, [], True),
        (20, 0, [0], False),
        (0, 1024**3 - 1, [1], True),
        (0, 1024**3, [], True),
        (0, 1024**3, [1], False),
        (0, 0, [-1], False),
        (0, 0, [None], False),
    ],
)
def test_validate_batch_capacity_enforces_exact_count_and_byte_boundaries(
    existing_files: int,
    existing_bytes: int,
    incoming: list[int | None],
    should_pass: bool,
) -> None:
    if should_pass:
        validate_batch_capacity(existing_files, existing_bytes, incoming)
    else:
        with pytest.raises(FileIntakeFailure) as exc_info:
            validate_batch_capacity(existing_files, existing_bytes, incoming)
        assert exc_info.value.code == "batch_capacity_exceeded"


@pytest.mark.asyncio
async def test_intake_deduplicates_equal_content_only_within_the_same_batch(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "data")
    first_batch = str(uuid4())
    second_batch = str(uuid4())
    payload = _pdf_bytes()
    first = await service.ingest_upload(
        first_batch, IncomingFile("one.pdf", "application/pdf", _chunks(payload))
    )
    duplicate = await service.ingest_upload(
        first_batch, IncomingFile("two.PDF", "application/pdf", _chunks(payload))
    )
    other_batch = await service.ingest_upload(
        second_batch, IncomingFile("three.pdf", "application/pdf", _chunks(payload))
    )

    assert duplicate == first
    assert other_batch.file_id != first.file_id
    assert len(list((tmp_path / "data" / first_batch / "input").glob("*.pdf"))) == 1
    assert len(list((tmp_path / "data" / second_batch / "input").glob("*.pdf"))) == 1


@pytest.mark.asyncio
async def test_failed_file_cleans_only_itself_and_preserves_successful_files(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "data")
    batch_id = str(uuid4())
    successful = await service.ingest_upload(
        batch_id,
        IncomingFile("image.png", "image/png", _chunks(_png_bytes())),
    )

    with pytest.raises(FileIntakeFailure):
        await service.ingest_upload(
            batch_id,
            IncomingFile("broken.png", "image/png", _chunks(b"truncated")),
        )

    assert successful.path.exists()
    assert not list(successful.path.parent.glob("*.part"))
    assert list(successful.path.parent.glob("*")) == [successful.path]


@pytest.mark.asyncio
async def test_intake_enforces_actual_batch_bytes_while_streaming(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    batch_id = str(uuid4())
    input_dir = data_root / batch_id / "input"
    input_dir.mkdir(parents=True)
    existing = input_dir / f"{uuid4()}.pdf"
    existing.write_bytes(b"12345")
    payload = _pdf_bytes()
    service = _service(
        data_root,
        max_file_size_bytes=len(payload),
        max_batch_size_bytes=5 + len(payload) - 1,
    )

    with pytest.raises(FileIntakeFailure) as exc_info:
        await service.ingest_upload(
            batch_id,
            IncomingFile("document.pdf", "application/pdf", _chunks(payload)),
        )

    assert exc_info.value.code == "file_too_large"
    assert existing.exists()
    assert not list(input_dir.glob("*.part"))


@pytest.mark.asyncio
async def test_intake_rejects_a_twenty_first_file_before_consuming_stream(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    batch_id = str(uuid4())
    input_dir = data_root / batch_id / "input"
    input_dir.mkdir(parents=True)
    for _ in range(20):
        (input_dir / f"{uuid4()}.pdf").write_bytes(b"x")
    consumed = False

    async def observed() -> AsyncIterator[bytes]:
        nonlocal consumed
        consumed = True
        yield _pdf_bytes()

    with pytest.raises(FileIntakeFailure) as exc_info:
        await _service(data_root).ingest_upload(
            batch_id,
            IncomingFile("document.pdf", "application/pdf", observed()),
        )

    assert exc_info.value.code == "batch_capacity_exceeded"
    assert consumed is False


@pytest.mark.asyncio
async def test_two_service_instances_cannot_publish_past_shared_file_limit(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    batch_id = str(uuid4())
    first = _service(data_root, max_files=1)
    second = _service(data_root, max_files=1)
    first_payload = _pdf_bytes()
    second_payload = first_payload + b"\n% second distinct payload"

    async def interleaved(payload: bytes) -> AsyncIterator[bytes]:
        midpoint = len(payload) // 2
        yield payload[:midpoint]
        await asyncio.sleep(0.05)
        yield payload[midpoint:]

    results = await asyncio.gather(
        first.ingest_upload(
            batch_id,
            IncomingFile("first.pdf", "application/pdf", interleaved(first_payload)),
        ),
        second.ingest_upload(
            batch_id,
            IncomingFile("second.pdf", "application/pdf", interleaved(second_payload)),
        ),
        return_exceptions=True,
    )

    stored = [result for result in results if not isinstance(result, BaseException)]
    rejected = [result for result in results if isinstance(result, FileIntakeFailure)]
    assert len(stored) == 1
    assert len(rejected) == 1
    assert rejected[0].code == "batch_capacity_exceeded"
    assert len(list((data_root / batch_id / "input").glob("*.pdf"))) == 1


@pytest.mark.asyncio
async def test_concurrent_equal_hash_uses_batch_scoped_deterministic_uuid(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    batch_id = str(uuid4())
    payload = _pdf_bytes()
    expected_id = str(uuid5(UUID(batch_id), hashlib.sha256(payload).hexdigest()))
    services = (_service(data_root), _service(data_root))

    async def interleaved() -> AsyncIterator[bytes]:
        midpoint = len(payload) // 2
        yield payload[:midpoint]
        await asyncio.sleep(0.05)
        yield payload[midpoint:]

    first, second = await asyncio.gather(
        services[0].ingest_upload(
            batch_id, IncomingFile("first.pdf", "application/pdf", interleaved())
        ),
        services[1].ingest_upload(
            batch_id, IncomingFile("second.pdf", "application/pdf", interleaved())
        ),
    )

    assert first == second
    assert first.file_id == expected_id
    assert len(list((data_root / batch_id / "input").glob("*.pdf"))) == 1


def test_process_shared_lock_prevents_cross_process_file_limit_race(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    ready_queue = context.Queue()
    result_queue = context.Queue()
    data_root = tmp_path / "data"
    batch_id = str(uuid4())
    first_payload = _pdf_bytes()
    second_payload = first_payload + b"\n% distinct subprocess payload"
    processes = [
        context.Process(
            target=_subprocess_ingest,
            args=(
                str(data_root),
                batch_id,
                payload,
                display_name,
                start_event,
                ready_queue,
                result_queue,
            ),
        )
        for payload, display_name in (
            (first_payload, "first.pdf"),
            (second_payload, "second.pdf"),
        )
    ]
    try:
        for process in processes:
            process.start()
        assert [ready_queue.get(timeout=10) for _ in processes] == ["ready", "ready"]
        start_event.set()
        results = [result_queue.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert sorted(result[0] for result in results) == ["error", "stored"]
    assert next(result[1] for result in results if result[0] == "error") == (
        "batch_capacity_exceeded"
    )
    assert len(list((data_root / batch_id / "input").glob("*.pdf"))) == 1
