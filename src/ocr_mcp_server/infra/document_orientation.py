"""Handle-safe immutable document correction implementations."""

from __future__ import annotations

from typing import BinaryIO

from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter

from ..domain.errors import FileIntakeFailure
from ..domain.files import SupportedMediaType
from ..domain.orientation import OrientationErrorCode, OrientationFailure
from ..domain.secondary_ocr import OrthogonalAngle
from ..services.file_storage import FileStorage
from ..services.file_validation import FileValidator
from ..services.orientation_recovery import OrientationCorrectionRequest


class ImmutableDocumentCorrector:
    """Apply detector-derived clockwise rotations into a new stored input."""

    def __init__(
        self,
        storage: FileStorage,
        *,
        max_file_size_bytes: int = 30 * 1024 * 1024,
        max_pages: int = 500,
        max_image_pixels: int = 100_000_000,
    ) -> None:
        self._storage = storage
        self._max_file_size_bytes = max_file_size_bytes
        self._validator = FileValidator(
            max_pages=max_pages, max_image_pixels=max_image_pixels
        )

    async def correct(self, request: OrientationCorrectionRequest):
        if not isinstance(request, OrientationCorrectionRequest):
            raise OrientationFailure(OrientationErrorCode.REQUEST_INVALID)
        transform = (
            self._pdf_transform(request)
            if request.media_type is SupportedMediaType.PDF
            else self._image_transform(request)
        )
        try:
            return await self._storage.create_immutable_derivative(
                request.batch_id,
                request.file_id,
                request.extension,
                expected_source_sha256=request.expected_source_sha256,
                expected_source_size_bytes=request.expected_source_size_bytes,
                transform=transform,
                max_file_size_bytes=self._max_file_size_bytes,
                validator=self._validator,
            )
        except FileIntakeFailure as exc:
            raise OrientationFailure(
                OrientationErrorCode.REQUEST_INVALID, cause=exc
            ) from None

    @staticmethod
    def _pdf_transform(request: OrientationCorrectionRequest):
        rotations = {
            item.page_number: int(item.angle) for item in request.decisions
        }

        def transform(source: BinaryIO, target: BinaryIO) -> None:
            reader = PdfReader(source, strict=True)
            if reader.is_encrypted or len(reader.pages) != request.page_count:
                raise ValueError("invalid source PDF")
            normalized_existing: list[int] = []
            for page in reader.pages:
                raw_rotation = page.get("/Rotate", 0)
                if (
                    isinstance(raw_rotation, bool)
                    or not isinstance(raw_rotation, int)
                    or raw_rotation % 90 != 0
                ):
                    raise ValueError("invalid raw page rotation")
                normalized_existing.append(int(raw_rotation) % 360)
            writer = PdfWriter()
            writer.clone_document_from_reader(reader)
            for page_number, page in enumerate(writer.pages, start=1):
                correction = rotations.get(page_number)
                if correction is not None:
                    existing = normalized_existing[page_number - 1]
                    page.rotation = (existing + correction) % 360
            writer.write(target)

        return transform

    @staticmethod
    def _image_transform(request: OrientationCorrectionRequest):
        angle = request.decisions[0].angle
        transpose = {
            OrthogonalAngle.DEG_90: Image.Transpose.ROTATE_270,
            OrthogonalAngle.DEG_180: Image.Transpose.ROTATE_180,
            OrthogonalAngle.DEG_270: Image.Transpose.ROTATE_90,
        }[angle]
        format_name = (
            "PNG" if request.media_type is SupportedMediaType.PNG else "JPEG"
        )

        def transform(source: BinaryIO, target: BinaryIO) -> None:
            with Image.open(source) as image:
                if image.format != format_name or request.page_count != 1:
                    raise ValueError("invalid source image")
                image.load()
                try:
                    oriented = ImageOps.exif_transpose(image)
                except (OSError, SyntaxError, ValueError):
                    oriented = image.copy()
                try:
                    corrected = oriented.transpose(transpose)
                    try:
                        save_options = (
                            {"optimize": True}
                            if format_name == "PNG"
                            else {"quality": 95}
                        )
                        corrected.save(target, format=format_name, **save_options)
                    finally:
                        corrected.close()
                finally:
                    if oriented is not image:
                        oriented.close()

        return transform
