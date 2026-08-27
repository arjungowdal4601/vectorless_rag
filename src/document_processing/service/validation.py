"""Read-only PDF acceptance checks run before a submission is registered."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID

import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pypdfium2.raw as pdfium_raw  # type: ignore[import-untyped]

from document_processing.errors import (
    EncryptedPdfError,
    InvalidPdfError,
    InvalidRequestError,
    PageLimitError,
    RenderSizeLimitError,
    RunNotFoundError,
    UnsupportedMediaTypeError,
)

PDF_MEDIA_TYPES = frozenset({"application/pdf", "application/x-pdf"})


def validate_run_id(run_id: str) -> str:
    """Require the canonical service-generated UUID representation."""

    try:
        parsed = UUID(run_id)
    except (ValueError, AttributeError) as exc:
        raise RunNotFoundError("The requested run does not exist.", run_id=run_id) from exc
    if str(parsed) != run_id:
        raise RunNotFoundError("The requested run does not exist.", run_id=run_id)
    return run_id


def validate_source_path(artifact_root: Path, run_id: str, source_path: Path) -> None:
    """Reject startup/resume sources outside their server-generated run path."""

    expected = artifact_root.resolve() / "runs" / run_id / "source" / "original.pdf"
    if source_path.is_symlink() or source_path.resolve() != expected:
        raise RuntimeError(f"source escaped the run directory for {run_id}")


def validate_submission(filename: str | None, content_type: str) -> str:
    """Return a path-free display filename for an accepted PDF media type."""

    media_type = content_type.partition(";")[0].strip().lower()
    if media_type not in PDF_MEDIA_TYPES:
        raise UnsupportedMediaTypeError("The upload must declare application/pdf.")
    if filename is None:
        raise InvalidRequestError("The PDF must have a filename.")
    safe_name = filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    if (
        not safe_name
        or safe_name in {".", ".."}
        or len(safe_name.encode("utf-8")) > 255
        or any(ord(character) < 32 for character in safe_name)
    ):
        raise InvalidRequestError("The PDF filename is invalid.")
    return safe_name


@runtime_checkable
class SubmissionValidator(Protocol):
    def validate(self, path: Path) -> int:
        """Return a positive page count or raise a sanitized input error."""


class PdfiumSubmissionValidator:
    """Open and inspect page geometry without extracting text or rendering."""

    def __init__(self, *, max_pages: int, max_page_pixels: int, dpi: int) -> None:
        self._max_pages = max_pages
        self._max_page_pixels = max_page_pixels
        self._scale = dpi / 72.0

    def validate(self, path: Path) -> int:
        try:
            document = pdfium.PdfDocument(path)
        except pdfium.PdfiumError as exc:
            if exc.err_code == pdfium_raw.FPDF_ERR_PASSWORD:
                raise EncryptedPdfError("Password-protected PDFs are not supported.") from exc
            raise InvalidPdfError("The source is not a readable PDF document.") from exc

        with document:
            page_count = len(document)
            if page_count == 0:
                raise InvalidPdfError("PDFs must contain at least one page.")
            if page_count > self._max_pages:
                raise PageLimitError(f"The PDF exceeds the {self._max_pages}-page limit.")
            for index in range(page_count):
                page = None
                try:
                    page = document[index]
                    width_points, height_points = page.get_size()
                    if not all(
                        math.isfinite(value) and value > 0
                        for value in (width_points, height_points)
                    ):
                        raise InvalidPdfError(f"PDF page {index + 1} has invalid dimensions.")
                    width = math.ceil(width_points * self._scale)
                    height = math.ceil(height_points * self._scale)
                    if width * height > self._max_page_pixels:
                        raise RenderSizeLimitError(
                            f"PDF page {index + 1} exceeds the rendered-page pixel limit."
                        )
                except (ValueError, pdfium.PdfiumError) as exc:
                    raise InvalidPdfError(f"PDF page {index + 1} is not readable.") from exc
                finally:
                    if page is not None:
                        page.close()
            return page_count
