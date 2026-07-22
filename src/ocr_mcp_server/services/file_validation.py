"""Decode and validate staged PDF and image files without retaining content."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import warnings

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

from ..domain.constants import SUPPORTED_EXTENSIONS
from ..domain.errors import FileIntakeErrorCode, FileIntakeFailure
from ..domain.files import SupportedMediaType


_EXTENSION_TYPES = {
    ".pdf": SupportedMediaType.PDF,
    ".png": SupportedMediaType.PNG,
    ".jpg": SupportedMediaType.JPEG,
    ".jpeg": SupportedMediaType.JPEG,
}
_MIME_TYPES = {
    "application/pdf": SupportedMediaType.PDF,
    "image/png": SupportedMediaType.PNG,
    "image/jpeg": SupportedMediaType.JPEG,
    "image/jpg": SupportedMediaType.JPEG,
}
_PILLOW_TYPES = {
    "PNG": SupportedMediaType.PNG,
    "JPEG": SupportedMediaType.JPEG,
}


@dataclass(frozen=True, slots=True)
class ValidatedFileMetadata:
    media_type: SupportedMediaType
    extension: str
    page_count: int
    width: int | None
    height: int | None


class FileValidator:
    """Validate extension, declared MIME, decoded format, and document limits."""

    def __init__(self, *, max_pages: int, max_image_pixels: int) -> None:
        self._max_pages = max_pages
        self._max_image_pixels = max_image_pixels

    def validate(
        self,
        path: Path,
        *,
        display_name: str,
        declared_mime: str,
    ) -> ValidatedFileMetadata:
        extension = Path(display_name).suffix.lower()
        normalized_mime = declared_mime.partition(";")[0].strip().lower()
        if extension not in SUPPORTED_EXTENSIONS or normalized_mime not in _MIME_TYPES:
            raise FileIntakeFailure(FileIntakeErrorCode.UNSUPPORTED_TYPE)

        expected_type = _EXTENSION_TYPES[extension]
        if _MIME_TYPES[normalized_mime] is not expected_type:
            raise FileIntakeFailure(FileIntakeErrorCode.MIME_MAGIC_MISMATCH)

        try:
            with path.open("rb") as stream:
                prefix = stream.read(16)
        except OSError:
            raise FileIntakeFailure(FileIntakeErrorCode.INVALID_DOCUMENT) from None
        if not prefix:
            raise FileIntakeFailure(FileIntakeErrorCode.INVALID_DOCUMENT)

        if expected_type is SupportedMediaType.PDF:
            if not prefix.startswith(b"%PDF-"):
                raise FileIntakeFailure(FileIntakeErrorCode.MIME_MAGIC_MISMATCH)
            return self._validate_pdf(path, extension)

        if prefix.startswith(b"%PDF-"):
            raise FileIntakeFailure(FileIntakeErrorCode.MIME_MAGIC_MISMATCH)
        return self._validate_image(path, extension, expected_type)

    def _validate_pdf(self, path: Path, extension: str) -> ValidatedFileMetadata:
        try:
            reader = PdfReader(path, strict=True)
            if reader.is_encrypted:
                raise FileIntakeFailure(FileIntakeErrorCode.ENCRYPTED_PDF)
            page_count = len(reader.pages)
        except FileIntakeFailure:
            raise
        except Exception:
            raise FileIntakeFailure(FileIntakeErrorCode.INVALID_DOCUMENT) from None

        if page_count < 1:
            raise FileIntakeFailure(FileIntakeErrorCode.INVALID_DOCUMENT)
        if page_count > self._max_pages:
            raise FileIntakeFailure(FileIntakeErrorCode.TOO_MANY_PAGES)
        return ValidatedFileMetadata(
            media_type=SupportedMediaType.PDF,
            extension=extension,
            page_count=page_count,
            width=None,
            height=None,
        )

    def _validate_image(
        self,
        path: Path,
        extension: str,
        expected_type: SupportedMediaType,
    ) -> ValidatedFileMetadata:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    image.verify()
                    decoded_type = _PILLOW_TYPES.get(image.format or "")
                if decoded_type is not expected_type:
                    raise FileIntakeFailure(
                        FileIntakeErrorCode.MIME_MAGIC_MISMATCH
                    )
                with Image.open(path) as image:
                    image.load()
                    if _PILLOW_TYPES.get(image.format or "") is not expected_type:
                        raise FileIntakeFailure(
                            FileIntakeErrorCode.MIME_MAGIC_MISMATCH
                        )
                    width, height = image.size
        except FileIntakeFailure:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise FileIntakeFailure(FileIntakeErrorCode.TOO_MANY_PIXELS) from None
        except (OSError, SyntaxError, ValueError, UnidentifiedImageError):
            raise FileIntakeFailure(FileIntakeErrorCode.INVALID_DOCUMENT) from None

        if width < 1 or height < 1:
            raise FileIntakeFailure(FileIntakeErrorCode.INVALID_DOCUMENT)
        if width * height > self._max_image_pixels:
            raise FileIntakeFailure(FileIntakeErrorCode.TOO_MANY_PIXELS)
        return ValidatedFileMetadata(
            media_type=expected_type,
            extension=extension,
            page_count=1,
            width=width,
            height=height,
        )
