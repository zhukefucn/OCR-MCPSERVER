from __future__ import annotations

from collections.abc import AsyncIterator
from array import array
from dataclasses import replace
from io import BytesIO
import asyncio
import io
import os
from pathlib import Path
import threading
from uuid import uuid4

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.constants import PageLabelStyle
from pypdf.generic import NameObject, NumberObject, TextStringObject

from ocr_mcp_server.domain.constants import DEFAULT_MAX_FILE_SIZE_BYTES
from ocr_mcp_server.domain.files import IncomingFile, SupportedMediaType
from ocr_mcp_server.domain.orientation import OrientationDecision, OrientationFailure
from ocr_mcp_server.domain.secondary_ocr import OrthogonalAngle
from ocr_mcp_server.infra.document_orientation import (
    ImmutableDocumentCorrector as _ImmutableDocumentCorrector,
)
from ocr_mcp_server.services.file_storage import BatchLockLease, FileStorage
from ocr_mcp_server.services.file_validation import FileValidator
from ocr_mcp_server.services.orientation_recovery import OrientationCorrectionRequest


async def _chunks(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


def test_corrector_default_file_limit_tracks_service_contract(tmp_path: Path) -> None:
    corrector = _ImmutableDocumentCorrector(FileStorage(tmp_path))
    assert corrector._max_file_size_bytes == DEFAULT_MAX_FILE_SIZE_BYTES


def _pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=144)
    writer.add_blank_page(width=100, height=200)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _image_bytes(format_name: str) -> bytes:
    image = Image.new("RGB", (2, 3), color=(10, 20, 30))
    image.putpixel((0, 0), (250, 1, 2))
    output = BytesIO()
    image.save(output, format=format_name, exif=b"unsafe-metadata")
    return output.getvalue()


def _decision(page: int, angle: OrthogonalAngle, *, credible: bool = True):
    return OrientationDecision(page, angle, 0.95, "layout", credible)


async def _stored(tmp_path: Path, payload: bytes, name: str, mime: str):
    storage = FileStorage(tmp_path / "data")
    batch_id = str(uuid4())
    stored = await storage.store(
        batch_id,
        IncomingFile(name, mime, _chunks(payload)),
        max_file_size_bytes=1_000_000,
        validator=FileValidator(max_pages=500, max_image_pixels=10_000),
    )
    return storage, batch_id, stored


class _Markers:
    async def bind_lock_marker(self, *args, **kwargs):
        return None

    async def bind_empty_lock_marker(
        self, *args, initialize, **kwargs
    ):
        initialize()


class ImmutableDocumentCorrector:
    """Exercise the production corrector under a real storage-minted lease."""

    _pdf_transform = staticmethod(_ImmutableDocumentCorrector._pdf_transform)
    _image_transform = staticmethod(_ImmutableDocumentCorrector._image_transform)

    def __init__(self, storage, **kwargs):
        self._storage = storage
        self._corrector = _ImmutableDocumentCorrector(storage, **kwargs)

    async def correct(self, request):
        self._corrector._pdf_transform = type(self)._pdf_transform
        self._corrector._image_transform = type(self)._image_transform
        if request.batch_lock is not None:
            return await self._corrector.correct(request)
        async with self._storage.batch_lock(
            request.batch_id,
            marker_registry=_Markers(),
            allow_missing_marker=True,
        ) as lease:
            return await self._corrector.correct(
                replace(request, batch_lock=lease)
            )


async def _correct_locked(storage, batch_id, corrector, request):
    async with storage.batch_lock(
        batch_id,
        marker_registry=_Markers(),
        allow_missing_marker=True,
    ) as lease:
        return await corrector.correct(replace(request, batch_lock=lease))


async def _create_derivative_locked(storage, batch_id, *args, **kwargs):
    async with storage.batch_lock(
        batch_id,
        marker_registry=_Markers(),
        allow_missing_marker=True,
    ) as lease:
        return await storage.create_immutable_derivative(
            batch_id, *args, batch_lock=lease, **kwargs
        )


def _request(batch_id: str, stored, *decisions: OrientationDecision):
    return OrientationCorrectionRequest(
        batch_id=batch_id,
        file_id=stored.file_id,
        media_type=stored.media_type,
        extension=stored.extension,
        page_count=stored.page_count,
        expected_source_sha256=stored.sha256,
        expected_source_size_bytes=stored.size_bytes,
        decisions=decisions,
        batch_lock=None,
    )


@pytest.mark.asyncio
async def test_correction_rejects_valid_same_page_replacement_with_different_bytes(
    tmp_path: Path,
) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    request = _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
    replacement = PdfWriter()
    replacement.add_blank_page(width=73, height=145)
    replacement.add_blank_page(width=101, height=201)
    output = BytesIO()
    replacement.write(output)
    stored.path.write_bytes(output.getvalue())

    with pytest.raises(OrientationFailure):
        await ImmutableDocumentCorrector(storage).correct(request)
    assert not any(
        path.name.startswith(stored.file_id) is False and path.suffix == ".pdf"
        for path in stored.path.parent.iterdir()
    )


@pytest.mark.asyncio
async def test_pdf_correction_adds_clockwise_display_rotation_without_mutating_source(
    tmp_path: Path,
) -> None:
    payload = _pdf_bytes()
    storage, batch_id, stored = await _stored(
        tmp_path, payload, "source.pdf", "application/pdf"
    )

    corrected = await ImmutableDocumentCorrector(storage).correct(
        _request(batch_id, stored, _decision(2, OrthogonalAngle.DEG_90))
    )

    assert stored.path.read_bytes() == payload
    assert corrected.file_id != stored.file_id
    assert corrected.path != stored.path
    reader = PdfReader(corrected.path)
    assert [page.rotation for page in reader.pages] == [0, 90]
    assert [(page.mediabox.width, page.mediabox.height) for page in reader.pages] == [
        (72, 144),
        (100, 200),
    ]


@pytest.mark.asyncio
async def test_pdf_correction_normalizes_existing_rotate_metadata(tmp_path: Path) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=72, height=144)
    page.rotate(270)
    output = BytesIO()
    writer.write(output)
    storage, batch_id, stored = await _stored(
        tmp_path, output.getvalue(), "source.pdf", "application/pdf"
    )

    corrected = await ImmutableDocumentCorrector(storage).correct(
        _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_180))
    )

    assert PdfReader(corrected.path).pages[0].rotation == 90


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "mime", "format_name", "angle", "expected_size"),
    [
        ("source.png", "image/png", "PNG", OrthogonalAngle.DEG_90, (3, 2)),
        ("source.jpg", "image/jpeg", "JPEG", OrthogonalAngle.DEG_270, (3, 2)),
    ],
)
async def test_image_correction_uses_clockwise_orthogonal_transpose_and_same_format(
    tmp_path: Path, name, mime, format_name, angle, expected_size
) -> None:
    payload = _image_bytes(format_name)
    storage, batch_id, stored = await _stored(tmp_path, payload, name, mime)

    corrected = await ImmutableDocumentCorrector(storage).correct(
        _request(batch_id, stored, _decision(1, angle))
    )

    assert stored.path.read_bytes() == payload
    with Image.open(corrected.path) as image:
        assert image.format == format_name
        assert image.size == expected_size
        assert "exif" not in image.info
        if format_name == "PNG" and angle is OrthogonalAngle.DEG_90:
            assert image.getpixel((2, 0)) == (250, 1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decisions",
    [
        (_decision(1, OrthogonalAngle.DEG_0),),
        (_decision(1, OrthogonalAngle.DEG_90, credible=False),),
        (_decision(3, OrthogonalAngle.DEG_90),),
        (_decision(1, OrthogonalAngle.DEG_90), _decision(1, OrthogonalAngle.DEG_180)),
    ],
)
async def test_invalid_uncertain_zero_or_out_of_range_correction_creates_no_output(
    tmp_path: Path, decisions
) -> None:
    payload = _pdf_bytes()
    storage, batch_id, stored = await _stored(
        tmp_path, payload, "source.pdf", "application/pdf"
    )

    with pytest.raises(ValueError, match="invalid orientation correction decisions"):
        request = _request(batch_id, stored, *decisions)
        await ImmutableDocumentCorrector(storage).correct(request)

    assert list(stored.path.parent.glob("*.pdf")) == [stored.path]
    assert not list(stored.path.parent.glob("*.part"))


@pytest.mark.asyncio
async def test_correction_request_has_no_path_angle_or_engine_control(tmp_path: Path) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    with pytest.raises(TypeError):
        OrientationCorrectionRequest(  # type: ignore[call-arg]
            batch_id=batch_id,
            file_id=stored.file_id,
            media_type=SupportedMediaType.PDF,
            extension=".pdf",
            page_count=2,
            expected_source_sha256=stored.sha256,
            expected_source_size_bytes=stored.size_bytes,
            decisions=(_decision(1, OrthogonalAngle.DEG_90),),
            batch_lock=None,
            output_path=tmp_path / "chosen.pdf",
        )


@pytest.mark.asyncio
async def test_pdf_page_count_binding_mismatch_creates_no_derivative(tmp_path: Path) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    request = OrientationCorrectionRequest(
        batch_id=batch_id,
        file_id=stored.file_id,
        media_type=stored.media_type,
        extension=stored.extension,
        page_count=1,
        expected_source_sha256=stored.sha256,
        expected_source_size_bytes=stored.size_bytes,
        decisions=(_decision(1, OrthogonalAngle.DEG_90),),
        batch_lock=None,
    )

    with pytest.raises(OrientationFailure) as caught:
        await ImmutableDocumentCorrector(storage).correct(request)

    assert caught.value.code == "orientation_request_invalid"
    assert list(stored.path.parent.glob("*.pdf")) == [stored.path]


@pytest.mark.asyncio
async def test_source_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows reparse coverage is exercised by FileStorage tests")
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(_pdf_bytes())
    stored.path.unlink()
    stored.path.symlink_to(outside)

    with pytest.raises(OrientationFailure) as caught:
        await ImmutableDocumentCorrector(storage).correct(
            _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
        )

    assert caught.value.code == "orientation_request_invalid"
    assert outside.read_bytes() == _pdf_bytes()
    assert not list(stored.path.parent.glob("*.part"))


@pytest.mark.asyncio
async def test_existing_server_named_target_is_never_replaced(tmp_path: Path) -> None:
    storage, batch_id, source = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    existing = await storage.store(
        batch_id,
        IncomingFile("existing.pdf", "application/pdf", _chunks(_pdf_bytes())),
        max_file_size_bytes=1_000_000,
        validator=FileValidator(max_pages=500, max_image_pixels=10_000),
    )
    existing_payload = existing.path.read_bytes()
    colliding_storage = FileStorage(tmp_path / "data", id_factory=lambda: existing.file_id)

    with pytest.raises(OrientationFailure) as caught:
        await ImmutableDocumentCorrector(colliding_storage).correct(
            _request(batch_id, source, _decision(1, OrthogonalAngle.DEG_90))
        )

    assert caught.value.code == "orientation_request_invalid"
    assert existing.path.read_bytes() == existing_payload
    assert not list(existing.path.parent.glob("*.part"))


@pytest.mark.asyncio
async def test_cleanup_does_not_delete_attacker_substituted_stage_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX dir-fd substitution regression")
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    original_assert = storage._assert_staged_identity
    attacker_payload = b"attacker-owned"
    substituted_path: Path | None = None

    def substitute(directory, name, descriptor, expected_identity):
        nonlocal substituted_path
        substituted_path = directory.path / name
        os.unlink(name, dir_fd=directory.descriptor)
        replacement = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory.descriptor,
        )
        try:
            os.write(replacement, attacker_payload)
        finally:
            os.close(replacement)
        original_assert(directory, name, descriptor, expected_identity)

    monkeypatch.setattr(storage, "_assert_staged_identity", substitute)
    with pytest.raises(OrientationFailure):
        await ImmutableDocumentCorrector(storage).correct(
            _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
        )

    assert substituted_path is not None
    assert substituted_path.read_bytes() == attacker_payload
    assert stored.path.exists()


@pytest.mark.asyncio
async def test_failure_after_publication_scrubs_only_the_held_derivative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )

    def fail_verification(*args, **kwargs):
        raise OSError("simulated post-publication verification failure")

    monkeypatch.setattr(storage, "_assert_published_identity", fail_verification)
    with pytest.raises(OrientationFailure):
        await ImmutableDocumentCorrector(storage).correct(
            _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
        )

    assert list(stored.path.parent.glob("*.pdf")) == [stored.path]
    assert not list(stored.path.parent.glob("*.part"))


@pytest.mark.asyncio
async def test_directory_change_after_publish_never_uses_unverified_name_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    original_unchanged = storage._directory_unchanged
    original_publish = storage._publish_no_replace
    original_cleanup = storage._cleanup_staged_name
    published = False
    cleanup_names: list[str] = []

    def changed_after_publish(directory):
        return False if published else original_unchanged(directory)

    def observed_publish(*args, **kwargs):
        nonlocal published
        original_publish(*args, **kwargs)
        published = True

    def observed_cleanup(directory, name, expected_identity, *, descriptor):
        cleanup_names.append(name)
        return original_cleanup(
            directory, name, expected_identity, descriptor=descriptor
        )

    monkeypatch.setattr(storage, "_directory_unchanged", changed_after_publish)
    monkeypatch.setattr(storage, "_publish_no_replace", observed_publish)
    monkeypatch.setattr(storage, "_cleanup_staged_name", observed_cleanup)
    with pytest.raises(OrientationFailure):
        await ImmutableDocumentCorrector(storage).correct(
            _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
        )
    assert len(cleanup_names) == 1
    assert cleanup_names[0].endswith(".pdf")
    assert not cleanup_names[0].endswith(".part")
    assert list(stored.path.parent.glob("*.pdf")) == [stored.path]


@pytest.mark.asyncio
async def test_cancellation_waits_for_worker_cleanup_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    entered = threading.Event()
    release = threading.Event()
    original = ImmutableDocumentCorrector._pdf_transform

    def blocked_transform(request):
        transform = original(request)

        def blocked(source, target):
            entered.set()
            assert release.wait(5)
            transform(source, target)

        return blocked

    monkeypatch.setattr(
        ImmutableDocumentCorrector, "_pdf_transform", staticmethod(blocked_transform)
    )
    task = asyncio.create_task(
        ImmutableDocumentCorrector(storage).correct(
            _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
        )
    )
    loop = asyncio.get_running_loop()
    unfinished_before_release: list[bool] = []

    def cancel_then_release():
        assert entered.wait(2)
        loop.call_soon_threadsafe(task.cancel)
        threading.Event().wait(0.025)
        loop.call_soon_threadsafe(task.cancel)
        threading.Event().wait(0.025)
        unfinished_before_release.append(not task.done())
        release.set()

    controller = threading.Thread(target=cancel_then_release)
    controller.start()
    with pytest.raises(asyncio.CancelledError):
        await task
    controller.join(timeout=2)
    assert unfinished_before_release == [True]
    assert list(stored.path.parent.glob("*.pdf")) == [stored.path]
    assert not list(stored.path.parent.glob("*.part"))


@pytest.mark.asyncio
async def test_cancellation_after_worker_publication_rolls_back_held_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    published = threading.Event()
    release = threading.Event()
    original = storage._create_immutable_derivative_sync

    def delayed_return(*args, **kwargs):
        outcome = original(*args, **kwargs)
        published.set()
        assert release.wait(5)
        return outcome

    monkeypatch.setattr(storage, "_create_immutable_derivative_sync", delayed_return)
    task = asyncio.create_task(
        ImmutableDocumentCorrector(storage).correct(
            _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
        )
    )
    assert await asyncio.to_thread(published.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(stored.path.parent.glob("*.pdf")) == [stored.path]


@pytest.mark.asyncio
async def test_transform_baseexception_preserves_control_flow_and_cleans_stage(
    tmp_path: Path,
) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )

    class _Stop(BaseException):
        pass

    def stop(source, target):
        target.write(b"partial")
        raise _Stop

    with pytest.raises(_Stop):
        await _create_derivative_locked(
            storage,
            batch_id,
            stored.file_id,
            stored.extension,
            expected_source_sha256=stored.sha256,
            expected_source_size_bytes=stored.size_bytes,
            transform=stop,
            max_file_size_bytes=1_000_000,
            validator=FileValidator(max_pages=500, max_image_pixels=10_000),
        )
    assert list(stored.path.parent.glob("*.pdf")) == [stored.path]
    assert not list(stored.path.parent.glob("*.part"))


@pytest.mark.asyncio
async def test_writer_enforces_byte_cap_before_oversized_write(tmp_path: Path) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    valid = _pdf_bytes()
    observed = False

    def transform(source, target):
        nonlocal observed
        with pytest.raises(Exception) as caught:
            target.write(b"x" * (len(valid) + 1))
        observed = getattr(caught.value, "code", None) == "file_too_large"
        target.seek(0)
        target.truncate()
        target.write(valid)

    corrected = await _create_derivative_locked(
        storage,
        batch_id,
        stored.file_id,
        stored.extension,
        expected_source_sha256=stored.sha256,
        expected_source_size_bytes=stored.size_bytes,
        transform=transform,
        max_file_size_bytes=len(valid),
        validator=FileValidator(max_pages=500, max_image_pixels=10_000),
    )
    assert observed is True
    assert corrected.size_bytes == len(valid)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["writelines", "multibyte", "noncontiguous"])
async def test_all_writer_surfaces_enforce_byte_cap_before_growth(
    tmp_path: Path, mode: str
) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    valid = _pdf_bytes()
    observed = False

    def transform(source, target):
        nonlocal observed
        with pytest.raises(Exception) as caught:
            if mode == "writelines":
                target.writelines((b"x" * len(valid), b"y"))
            elif mode == "multibyte":
                target.write(memoryview(array("I", [0x01020304])))
            else:
                target.write(memoryview(bytearray(b"abcdef"))[::2])
        observed = getattr(caught.value, "code", None) == "file_too_large"
        target.seek(0)
        target.truncate()
        target.write(valid)

    cap = len(valid) if mode == "writelines" else 3
    if mode == "multibyte":
        valid = _pdf_bytes()
        cap = len(valid)
        oversized = array("I", [0] * ((cap // 4) + 1))

        def transform(source, target):
            nonlocal observed
            with pytest.raises(Exception) as caught:
                target.write(memoryview(oversized))
            observed = getattr(caught.value, "code", None) == "file_too_large"
            target.write(valid)
    elif mode == "noncontiguous":
        cap = len(valid)

        def transform(source, target):
            nonlocal observed
            with pytest.raises(TypeError, match="non-contiguous"):
                target.write(memoryview(bytearray(b"abcdef"))[::2])
            observed = target.tell() == 0
            target.write(valid)

    corrected = await _create_derivative_locked(
        storage,
        batch_id,
        stored.file_id,
        stored.extension,
        expected_source_sha256=stored.sha256,
        expected_source_size_bytes=stored.size_bytes,
        transform=transform,
        max_file_size_bytes=cap,
        validator=FileValidator(max_pages=500, max_image_pixels=10_000),
    )
    assert observed is True
    assert corrected.size_bytes == len(valid)


@pytest.mark.asyncio
async def test_writer_exposes_no_descriptor_or_raw_mutator_bypass(tmp_path: Path) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    valid = _pdf_bytes()

    def transform(source, target):
        with pytest.raises(io.UnsupportedOperation):
            target.fileno()
        with pytest.raises(AttributeError):
            target.raw
        target.write(valid)

    corrected = await _create_derivative_locked(
        storage,
        batch_id,
        stored.file_id,
        stored.extension,
        expected_source_sha256=stored.sha256,
        expected_source_size_bytes=stored.size_bytes,
        transform=transform,
        max_file_size_bytes=len(valid),
        validator=FileValidator(max_pages=500, max_image_pixels=10_000),
    )
    assert corrected.size_bytes == len(valid)


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [0, -1, True, False])
async def test_derivative_rejects_non_positive_or_boolean_byte_cap(
    tmp_path: Path, cap
) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    called = False

    def transform(source, target):
        nonlocal called
        called = True

    with pytest.raises(Exception) as caught:
        await _create_derivative_locked(
            storage,
            batch_id,
            stored.file_id,
            stored.extension,
            expected_source_sha256=stored.sha256,
            expected_source_size_bytes=stored.size_bytes,
            transform=transform,
            max_file_size_bytes=cap,
            validator=FileValidator(max_pages=500, max_image_pixels=10_000),
        )
    assert getattr(caught.value, "code", None) == "file_too_large"
    assert called is False


@pytest.mark.asyncio
async def test_stage_with_unexpected_hardlink_is_rejected_and_scrubbed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX staged hardlink regression")
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    original = storage._assert_staged_identity
    attacker_link: Path | None = None

    def add_hardlink(directory, name, descriptor, expected_identity):
        nonlocal attacker_link
        original(directory, name, descriptor, expected_identity)
        attacker_link = directory.path / "attacker-link"
        os.link(
            name,
            attacker_link.name,
            src_dir_fd=directory.descriptor,
            dst_dir_fd=directory.descriptor,
            follow_symlinks=False,
        )

    monkeypatch.setattr(storage, "_assert_staged_identity", add_hardlink)
    with pytest.raises(OrientationFailure):
        await ImmutableDocumentCorrector(storage).correct(
            _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
        )
    assert attacker_link is not None
    assert attacker_link.read_bytes() == b""


@pytest.mark.asyncio
async def test_pdf_clone_preserves_catalog_metadata_and_outlines(tmp_path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=144)
    writer.add_blank_page(width=100, height=200)
    writer.add_metadata({"/Title": "catalog-title", "/Author": "catalog-author"})
    writer.add_outline_item("second-page", 1)
    writer.set_page_label(0, 1, style=PageLabelStyle.DECIMAL, prefix="BANK-", start=1)
    output = BytesIO()
    writer.write(output)
    storage, batch_id, stored = await _stored(
        tmp_path, output.getvalue(), "source.pdf", "application/pdf"
    )

    corrected = await ImmutableDocumentCorrector(storage).correct(
        _request(batch_id, stored, _decision(2, OrthogonalAngle.DEG_90))
    )

    reader = PdfReader(corrected.path)
    assert reader.metadata.title == "catalog-title"
    assert reader.metadata.author == "catalog-author"
    assert reader.outline[0].title == "second-page"
    assert reader.page_labels == ["BANK-1", "BANK-2"]


@pytest.mark.asyncio
async def test_source_hardlink_added_after_open_aborts_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX source hardlink regression")
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    original = ImmutableDocumentCorrector._pdf_transform
    attacker_link = stored.path.parent / "source-hardlink"

    def linked_transform(request):
        transform = original(request)

        def linked(source, target):
            os.link(stored.path, attacker_link)
            transform(source, target)

        return linked

    monkeypatch.setattr(
        ImmutableDocumentCorrector, "_pdf_transform", staticmethod(linked_transform)
    )
    with pytest.raises(OrientationFailure):
        await ImmutableDocumentCorrector(storage).correct(
            _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
        )
    assert list(stored.path.parent.glob("*.part")) == []
    assert len(list(stored.path.parent.glob("*.pdf"))) == 1


@pytest.mark.asyncio
async def test_source_name_substitution_after_open_preserves_attacker_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX source substitution regression")
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    original = ImmutableDocumentCorrector._pdf_transform
    attacker_payload = b"attacker-source-name"

    def substituted_transform(request):
        transform = original(request)

        def substituted(source, target):
            stored.path.unlink()
            stored.path.write_bytes(attacker_payload)
            transform(source, target)

        return substituted

    monkeypatch.setattr(
        ImmutableDocumentCorrector,
        "_pdf_transform",
        staticmethod(substituted_transform),
    )
    with pytest.raises(OrientationFailure):
        await ImmutableDocumentCorrector(storage).correct(
            _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
        )
    assert stored.path.read_bytes() == attacker_payload
    assert not list(stored.path.parent.glob("*.part"))


@pytest.mark.asyncio
async def test_published_hardlink_before_final_check_is_scrubbed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX published hardlink regression")
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    original = storage._assert_published_identity
    attacker_link = stored.path.parent / "published-hardlink"

    def add_link(directory, name, descriptor, expected_identity):
        original(directory, name, descriptor, expected_identity)
        os.link(
            name,
            attacker_link.name,
            src_dir_fd=directory.descriptor,
            dst_dir_fd=directory.descriptor,
            follow_symlinks=False,
        )

    monkeypatch.setattr(storage, "_assert_published_identity", add_link)
    with pytest.raises(OrientationFailure):
        await ImmutableDocumentCorrector(storage).correct(
            _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
        )
    assert attacker_link.read_bytes() == b""


@pytest.mark.asyncio
async def test_published_name_substitution_preserves_attacker_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX published substitution regression")
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    original = storage._assert_published_identity
    attacker_payload = b"attacker-published-name"
    attacker_path: Path | None = None

    def substitute(directory, name, descriptor, expected_identity):
        nonlocal attacker_path
        attacker_path = directory.path / name
        os.unlink(name, dir_fd=directory.descriptor)
        replacement = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory.descriptor,
        )
        try:
            os.write(replacement, attacker_payload)
        finally:
            os.close(replacement)
        original(directory, name, descriptor, expected_identity)

    monkeypatch.setattr(storage, "_assert_published_identity", substitute)
    with pytest.raises(OrientationFailure):
        await ImmutableDocumentCorrector(storage).correct(
            _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
        )
    assert attacker_path is not None
    assert attacker_path.read_bytes() == attacker_payload


@pytest.mark.asyncio
async def test_late_cancellation_retries_held_rollback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    published = threading.Event()
    release = threading.Event()
    original_worker = storage._create_immutable_derivative_sync
    original_ftruncate = os.ftruncate
    attempts = 0

    def delayed_return(*args, **kwargs):
        outcome = original_worker(*args, **kwargs)
        published.set()
        assert release.wait(5)
        return outcome

    def flaky_ftruncate(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated transient rollback failure")
        return original_ftruncate(*args, **kwargs)

    monkeypatch.setattr(storage, "_create_immutable_derivative_sync", delayed_return)
    monkeypatch.setattr(os, "ftruncate", flaky_ftruncate)
    task = asyncio.create_task(
        ImmutableDocumentCorrector(storage).correct(
            _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
        )
    )
    assert await asyncio.to_thread(published.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts == 2
    assert list(stored.path.parent.glob("*.pdf")) == [stored.path]


@pytest.mark.asyncio
async def test_commit_is_a_non_awaiting_event_loop_linearization_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    event_loop_thread = threading.get_ident()
    commit_threads: list[int] = []
    entered = threading.Event()
    release = threading.Event()
    original = storage._commit_immutable_derivative_sync

    def observed_commit(outcome):
        commit_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        original(outcome)

    monkeypatch.setattr(storage, "_commit_immutable_derivative_sync", observed_commit)
    task = asyncio.create_task(
        ImmutableDocumentCorrector(storage).correct(
            _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
        )
    )
    loop = asyncio.get_running_loop()

    def cancel_during_commit():
        assert entered.wait(2)
        loop.call_soon_threadsafe(task.cancel)
        release.set()

    controller = threading.Thread(target=cancel_during_commit)
    controller.start()
    corrected = await task
    controller.join(timeout=2)

    assert commit_threads == [event_loop_thread]
    assert corrected.path.exists()


@pytest.mark.asyncio
async def test_committed_result_survives_descriptor_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    original_commit = storage._commit_immutable_derivative_sync
    original_close = os.close
    in_commit = False
    failed_once = False

    def observed_commit(outcome):
        nonlocal in_commit
        in_commit = True
        try:
            original_commit(outcome)
        finally:
            in_commit = False

    def failed_close(descriptor):
        nonlocal failed_once
        if in_commit and not failed_once:
            failed_once = True
            original_close(descriptor)
            raise OSError("simulated ambiguous descriptor close failure")
        return original_close(descriptor)

    monkeypatch.setattr(storage, "_commit_immutable_derivative_sync", observed_commit)
    monkeypatch.setattr(os, "close", failed_close)
    corrected = await ImmutableDocumentCorrector(storage).correct(
        _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
    )

    assert failed_once is True
    assert corrected.path.exists()
    assert PdfReader(corrected.path).pages[0].rotation == 90


@pytest.mark.asyncio
async def test_committed_result_survives_directory_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    close_attempts = 0
    original_close = storage._close_held_directory

    def failed_directory_close(directory):
        nonlocal close_attempts
        close_attempts += 1
        original_close(directory)
        return False

    monkeypatch.setattr(storage, "_close_held_directory", failed_directory_close)
    corrected = await ImmutableDocumentCorrector(storage).correct(
        _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
    )

    assert close_attempts == 1
    assert corrected.path.exists()
    assert PdfReader(corrected.path).pages[0].rotation == 90


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rotate",
    [TextStringObject("90"), NumberObject(45)],
)
async def test_pdf_rejects_non_integer_or_non_orthogonal_raw_rotate(
    tmp_path: Path, rotate
) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=72, height=144)
    page[NameObject("/Rotate")] = rotate
    output = BytesIO()
    writer.write(output)
    storage, batch_id, stored = await _stored(
        tmp_path, output.getvalue(), "source.pdf", "application/pdf"
    )

    with pytest.raises(OrientationFailure):
        await ImmutableDocumentCorrector(storage).correct(
            _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
        )
    assert list(stored.path.parent.glob("*.pdf")) == [stored.path]


@pytest.mark.asyncio
async def test_image_applies_exif_orientation_before_clockwise_detector_rotation(
    tmp_path: Path,
) -> None:
    image = Image.new("RGB", (2, 3), color=(10, 20, 30))
    image.putpixel((0, 0), (250, 1, 2))
    exif = Image.Exif()
    exif[274] = 6
    output = BytesIO()
    image.save(output, format="JPEG", quality=100, subsampling=0, exif=exif)
    storage, batch_id, stored = await _stored(
        tmp_path, output.getvalue(), "source.jpg", "image/jpeg"
    )

    corrected = await ImmutableDocumentCorrector(storage).correct(
        _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90))
    )

    with Image.open(corrected.path) as result:
        assert result.size == (2, 3)
        assert "exif" not in result.info


@pytest.mark.asyncio
async def test_immutable_correction_counts_existing_and_staged_bytes_at_batch_limit(
    tmp_path: Path,
) -> None:
    payload = _pdf_bytes()
    reference_storage, reference_batch, reference = await _stored(
        tmp_path / "reference", payload, "source.pdf", "application/pdf"
    )
    reference_corrected = await _correct_locked(
        reference_storage,
        reference_batch,
        ImmutableDocumentCorrector(reference_storage),
        _request(
            reference_batch,
            reference,
            _decision(1, OrthogonalAngle.DEG_90),
        )
    )
    exact_limit = reference.size_bytes + reference_corrected.size_bytes

    exact_storage, exact_batch, exact = await _stored(
        tmp_path / "exact", payload, "source.pdf", "application/pdf"
    )
    accepted = await _correct_locked(
        exact_storage,
        exact_batch,
        ImmutableDocumentCorrector(
            exact_storage, max_batch_size_bytes=exact_limit
        ),
        _request(exact_batch, exact, _decision(1, OrthogonalAngle.DEG_90))
    )
    assert exact_storage.batch_usage(exact_batch).total_bytes == exact_limit
    assert accepted.size_bytes == reference_corrected.size_bytes

    rejected_storage, rejected_batch, rejected = await _stored(
        tmp_path / "rejected", payload, "source.pdf", "application/pdf"
    )
    with pytest.raises(OrientationFailure):
        await _correct_locked(
            rejected_storage,
            rejected_batch,
            ImmutableDocumentCorrector(
                rejected_storage, max_batch_size_bytes=exact_limit - 1
            ),
            _request(
                rejected_batch,
                rejected,
                _decision(1, OrthogonalAngle.DEG_90),
            )
        )
    assert rejected_storage.batch_usage(rejected_batch).total_bytes == rejected.size_bytes


@pytest.mark.asyncio
async def test_concurrent_immutable_corrections_cannot_jointly_exceed_batch_limit(
    tmp_path: Path,
) -> None:
    storage = FileStorage(tmp_path / "data")
    batch_id = str(uuid4())
    first_payload = _pdf_bytes()
    writer = PdfWriter()
    writer.add_blank_page(width=80, height=160)
    writer.add_blank_page(width=110, height=210)
    second_output = BytesIO()
    writer.write(second_output)
    validator = FileValidator(max_pages=500, max_image_pixels=10_000)
    first = await storage.store(
        batch_id,
        IncomingFile("first.pdf", "application/pdf", _chunks(first_payload)),
        max_file_size_bytes=1_000_000,
        validator=validator,
    )
    second = await storage.store(
        batch_id,
        IncomingFile(
            "second.pdf", "application/pdf", _chunks(second_output.getvalue())
        ),
        max_file_size_bytes=1_000_000,
        validator=validator,
    )
    # Either correction can fit by itself, but both cannot fit together.
    batch_limit = (
        first.size_bytes
        + second.size_bytes
        + max(first.size_bytes, second.size_bytes)
        + 128
    )
    corrector = ImmutableDocumentCorrector(
        storage, max_batch_size_bytes=batch_limit
    )
    outcomes = await asyncio.gather(
        _correct_locked(
            storage,
            batch_id,
            corrector,
            _request(batch_id, first, _decision(1, OrthogonalAngle.DEG_90))
        ),
        _correct_locked(
            storage,
            batch_id,
            corrector,
            _request(batch_id, second, _decision(1, OrthogonalAngle.DEG_90))
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(item, OrientationFailure) for item in outcomes) == 1
    assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
    assert storage.batch_usage(batch_id).total_bytes <= batch_limit


@pytest.mark.asyncio
async def test_forged_batch_lease_cannot_publish_derivative(tmp_path: Path) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    with pytest.raises(TypeError):
        BatchLockLease(batch_id, -1, lambda: True)
    forged = object.__new__(BatchLockLease)
    request = replace(
        _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90)),
        batch_lock=forged,
    )

    with pytest.raises(OrientationFailure):
        await ImmutableDocumentCorrector(storage).correct(request)
    assert storage.batch_usage(batch_id).file_count == 1


@pytest.mark.asyncio
async def test_batch_lease_is_storage_bound_and_invalid_immediately_after_exit(
    tmp_path: Path,
) -> None:
    storage, batch_id, stored = await _stored(
        tmp_path, _pdf_bytes(), "source.pdf", "application/pdf"
    )
    other_instance = FileStorage(storage._data_root)
    async with storage.batch_lock(
        batch_id,
        marker_registry=_Markers(),
        allow_missing_marker=True,
    ) as lease:
        request = replace(
            _request(batch_id, stored, _decision(1, OrthogonalAngle.DEG_90)),
            batch_lock=lease,
        )
        with pytest.raises(OrientationFailure):
            await ImmutableDocumentCorrector(other_instance).correct(request)

    with pytest.raises(OrientationFailure):
        await ImmutableDocumentCorrector(storage).correct(request)
    assert storage.batch_usage(batch_id).file_count == 1
