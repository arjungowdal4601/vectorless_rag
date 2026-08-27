"""Public PDF intake and rendering interfaces."""

from .errors import (
    EncryptedPdfError,
    InvalidPdfSignatureError,
    MalformedPdfError,
    PdfArtifactConflictError,
    PdfPageLimitError,
    PdfProcessingError,
    PdfRenderError,
    PdfStorageError,
    PdfUploadTooLargeError,
    RenderedPagePixelLimitError,
    RenderIntegrityError,
    UnsupportedPdfMediaTypeError,
    ZeroPagePdfError,
)
from .models import PdfRenderConfig, PreparedPdf, RenderedPage, RenderManifest, StoredPdf
from .renderer import PdfIntakeService

__all__ = [
    "EncryptedPdfError",
    "InvalidPdfSignatureError",
    "MalformedPdfError",
    "PdfArtifactConflictError",
    "PdfIntakeService",
    "PdfPageLimitError",
    "PdfProcessingError",
    "PdfRenderConfig",
    "PdfRenderError",
    "PdfStorageError",
    "PdfUploadTooLargeError",
    "PreparedPdf",
    "RenderedPage",
    "RenderedPagePixelLimitError",
    "RenderIntegrityError",
    "RenderManifest",
    "StoredPdf",
    "UnsupportedPdfMediaTypeError",
    "ZeroPagePdfError",
]
