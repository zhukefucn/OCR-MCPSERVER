from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin
from pypdf import PdfWriter

from ocr_mcp_server.domain.errors import FileIntakeFailure
from ocr_mcp_server.domain.files import SupportedMediaType
from ocr_mcp_server.services.file_validation import FileValidator


def _write_pdf(path: Path, pages: int, *, encrypted: bool = False) -> None:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    if encrypted:
        writer.encrypt("secret")
    with path.open("wb") as stream:
        writer.write(stream)


def _write_image(path: Path, image_format: str, size: tuple[int, int]) -> None:
    Image.new("RGB", size, color=(12, 34, 56)).save(path, format=image_format)


def _assert_code(exc_info: pytest.ExceptionInfo[FileIntakeFailure], code: str) -> None:
    assert exc_info.value.code == code


@pytest.mark.parametrize("pages", [1, 500])
def test_validator_accepts_real_pdf_page_boundaries(tmp_path: Path, pages: int) -> None:
    path = tmp_path / "document.part"
    _write_pdf(path, pages)

    metadata = FileValidator(max_pages=500, max_image_pixels=100).validate(
        path, display_name="document.PDF", declared_mime="application/pdf; charset=binary"
    )

    assert metadata.media_type is SupportedMediaType.PDF
    assert metadata.extension == ".pdf"
    assert metadata.page_count == pages
    assert metadata.width is None
    assert metadata.height is None


def test_validator_rejects_pdf_above_page_limit(tmp_path: Path) -> None:
    path = tmp_path / "document.part"
    _write_pdf(path, 501)

    with pytest.raises(FileIntakeFailure) as exc_info:
        FileValidator(max_pages=500, max_image_pixels=100).validate(
            path, display_name="document.pdf", declared_mime="application/pdf"
        )

    _assert_code(exc_info, "pdf_too_many_pages")


def test_validator_rejects_encrypted_pdf_without_trying_a_password(tmp_path: Path) -> None:
    path = tmp_path / "document.part"
    _write_pdf(path, 1, encrypted=True)

    with pytest.raises(FileIntakeFailure) as exc_info:
        FileValidator(max_pages=500, max_image_pixels=100).validate(
            path, display_name="document.pdf", declared_mime="application/pdf"
        )

    _assert_code(exc_info, "pdf_encrypted")


@pytest.mark.parametrize("kind", ["empty", "zero_pages", "corrupt"])
def test_validator_rejects_invalid_pdf_documents(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "document.part"
    if kind == "zero_pages":
        _write_pdf(path, 0)
    elif kind == "corrupt":
        path.write_bytes(b"%PDF-1.7\nnot a valid pdf body")
    else:
        path.write_bytes(b"")

    with pytest.raises(FileIntakeFailure) as exc_info:
        FileValidator(max_pages=500, max_image_pixels=100).validate(
            path, display_name="document.pdf", declared_mime="application/pdf"
        )

    _assert_code(exc_info, "document_invalid")


@pytest.mark.parametrize(
    ("extension", "declared_mime", "image_format", "expected_type"),
    [
        (".png", "image/png", "PNG", SupportedMediaType.PNG),
        (".jpg", "image/jpeg", "JPEG", SupportedMediaType.JPEG),
        (".jpeg", "image/jpg; charset=binary", "JPEG", SupportedMediaType.JPEG),
    ],
)
def test_validator_decodes_real_images_and_normalizes_jpeg(
    tmp_path: Path,
    extension: str,
    declared_mime: str,
    image_format: str,
    expected_type: SupportedMediaType,
) -> None:
    path = tmp_path / "image.part"
    _write_image(path, image_format, (10, 10))

    metadata = FileValidator(max_pages=500, max_image_pixels=100).validate(
        path, display_name=f"image{extension.upper()}", declared_mime=declared_mime
    )

    assert metadata.media_type is expected_type
    assert metadata.extension == extension
    assert metadata.page_count == 1
    assert (metadata.width, metadata.height) == (10, 10)


def test_validator_rejects_one_pixel_above_configured_limit(tmp_path: Path) -> None:
    path = tmp_path / "image.part"
    _write_image(path, "PNG", (101, 1))

    with pytest.raises(FileIntakeFailure) as exc_info:
        FileValidator(max_pages=500, max_image_pixels=100).validate(
            path, display_name="image.png", declared_mime="image/png"
        )

    _assert_code(exc_info, "image_too_many_pixels")


def test_pixel_cap_rejects_before_pillow_verify_or_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "image.part"
    _write_image(path, "PNG", (101, 1))
    verify_called = False
    original_verify = PngImagePlugin.PngImageFile.verify

    def observed_verify(image) -> None:
        nonlocal verify_called
        verify_called = True
        original_verify(image)

    monkeypatch.setattr(PngImagePlugin.PngImageFile, "verify", observed_verify)

    with pytest.raises(FileIntakeFailure) as exc_info:
        FileValidator(max_pages=500, max_image_pixels=100).validate(
            path, display_name="image.png", declared_mime="image/png"
        )

    _assert_code(exc_info, "image_too_many_pixels")
    assert verify_called is False


def test_validator_converts_pillow_bomb_warning_to_safe_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "image.part"
    _write_image(path, "PNG", (11, 10))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)

    with pytest.raises(FileIntakeFailure) as exc_info:
        FileValidator(max_pages=500, max_image_pixels=1_000).validate(
            path, display_name="image.png", declared_mime="image/png"
        )

    _assert_code(exc_info, "image_too_many_pixels")


def test_validator_rejects_truncated_image(tmp_path: Path) -> None:
    path = tmp_path / "image.part"
    _write_image(path, "JPEG", (20, 20))
    path.write_bytes(path.read_bytes()[:-20])

    with pytest.raises(FileIntakeFailure) as exc_info:
        FileValidator(max_pages=500, max_image_pixels=1_000).validate(
            path, display_name="image.jpg", declared_mime="image/jpeg"
        )

    _assert_code(exc_info, "document_invalid")


@pytest.mark.parametrize(
    ("display_name", "declared_mime", "payload_kind", "expected_code"),
    [
        ("document.txt", "application/pdf", "pdf", "file_type_unsupported"),
        ("document.pdf", "text/plain", "pdf", "file_type_unsupported"),
        ("document.png", "image/jpeg", "png", "file_type_mismatch"),
        ("document.jpg", "image/jpeg", "png", "file_type_mismatch"),
        ("document.pdf", "application/pdf", "png", "file_type_mismatch"),
    ],
)
def test_validator_rejects_extension_mime_and_content_disagreement(
    tmp_path: Path,
    display_name: str,
    declared_mime: str,
    payload_kind: str,
    expected_code: str,
) -> None:
    path = tmp_path / "payload.part"
    if payload_kind == "pdf":
        _write_pdf(path, 1)
    else:
        _write_image(path, "PNG", (1, 1))

    with pytest.raises(FileIntakeFailure) as exc_info:
        FileValidator(max_pages=500, max_image_pixels=100).validate(
            path, display_name=display_name, declared_mime=declared_mime
        )

    _assert_code(exc_info, expected_code)


def test_validator_oserror_has_no_sensitive_exception_context(tmp_path: Path) -> None:
    missing = tmp_path / "sensitive-original-name.pdf"

    with pytest.raises(FileIntakeFailure) as exc_info:
        FileValidator(max_pages=500, max_image_pixels=100).validate(
            missing,
            display_name="document.pdf",
            declared_mime="application/pdf",
        )

    assert exc_info.value.code == "document_invalid"
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
