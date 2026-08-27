"""Submission-time PDFium validation tests."""

from pathlib import Path

import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pypdfium2.raw as pdfium_raw  # type: ignore[import-untyped]
import pytest

from document_processing.errors import (
    EncryptedPdfError,
    InvalidPdfError,
    PageLimitError,
    RenderSizeLimitError,
)
from document_processing.service.validation import PdfiumSubmissionValidator
from tests.pdf.helpers import PageSpec, make_pdf


def validator(*, max_pages: int = 10, max_pixels: int = 1_000_000) -> PdfiumSubmissionValidator:
    return PdfiumSubmissionValidator(max_pages=max_pages, max_page_pixels=max_pixels, dpi=200)


def test_validator_accepts_readable_nonempty_pdf(tmp_path: Path) -> None:
    path = tmp_path / "valid.pdf"
    make_pdf(path, (PageSpec(), PageSpec(rotation=90)))

    assert validator().validate(path) == 2


def test_validator_rejects_malformed_pdf(tmp_path: Path) -> None:
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"%PDF-not-really")

    with pytest.raises(InvalidPdfError):
        validator().validate(path)


def test_validator_rejects_page_limit(tmp_path: Path) -> None:
    path = tmp_path / "many.pdf"
    make_pdf(path, (PageSpec(), PageSpec()))

    with pytest.raises(PageLimitError):
        validator(max_pages=1).validate(path)


def test_validator_rejects_rendered_pixel_limit(tmp_path: Path) -> None:
    path = tmp_path / "large.pdf"
    make_pdf(path, (PageSpec(width_points=100, height_points=100),))

    with pytest.raises(RenderSizeLimitError):
        validator(max_pixels=100).validate(path)


def test_validator_classifies_password_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def encrypted(_path: Path) -> None:
        raise pdfium.PdfiumError("password", err_code=pdfium_raw.FPDF_ERR_PASSWORD)

    monkeypatch.setattr(pdfium, "PdfDocument", encrypted)

    with pytest.raises(EncryptedPdfError):
        validator().validate(Path("encrypted.pdf"))
