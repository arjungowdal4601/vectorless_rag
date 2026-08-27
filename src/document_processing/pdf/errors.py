"""Typed, sanitized failures raised by PDF intake and rendering."""

from __future__ import annotations


class PdfProcessingError(Exception):
    """Base class for expected PDF processing failures."""

    code = "pdf_processing_error"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class UnsupportedPdfMediaTypeError(PdfProcessingError):
    """The upload did not declare the supported PDF media type."""

    code = "unsupported_pdf_media_type"


class PdfUploadTooLargeError(PdfProcessingError):
    """The source exceeded the configured byte limit."""

    code = "pdf_upload_too_large"


class InvalidPdfSignatureError(PdfProcessingError):
    """The source did not begin with a PDF header."""

    code = "invalid_pdf_signature"


class MalformedPdfError(PdfProcessingError):
    """PDFium could not load the source as a PDF document."""

    code = "malformed_pdf"


class EncryptedPdfError(PdfProcessingError):
    """The source requires a password."""

    code = "encrypted_pdf"


class ZeroPagePdfError(PdfProcessingError):
    """The PDF has no pages."""

    code = "zero_page_pdf"


class PdfPageLimitError(PdfProcessingError):
    """The PDF exceeded the configured page count limit."""

    code = "pdf_page_limit_exceeded"


class RenderedPagePixelLimitError(PdfProcessingError):
    """A rendered page would exceed the configured pixel limit."""

    code = "rendered_page_pixel_limit_exceeded"


class PdfRenderError(PdfProcessingError):
    """A valid PDF page could not be rasterized."""

    code = "pdf_render_failed"


class PdfArtifactConflictError(PdfProcessingError):
    """Immutable output paths already exist for this run."""

    code = "pdf_artifact_conflict"


class PdfStorageError(PdfProcessingError):
    """A PDF artifact could not be durably stored."""

    code = "pdf_storage_failed"


class RenderIntegrityError(PdfProcessingError):
    """Persisted source, image, or manifest data failed verification."""

    code = "render_integrity_failed"
