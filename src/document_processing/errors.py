"""Application errors safe to map onto local API problem responses."""

from __future__ import annotations


class DocumentProcessingError(Exception):
    """Base error with a stable machine code and HTTP mapping hint."""

    code = "document_processing_error"
    status_code = 500

    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = message
        self.run_id = run_id


class ConfigurationError(DocumentProcessingError):
    code = "invalid_configuration"


class InvalidRequestError(DocumentProcessingError):
    code = "invalid_request"
    status_code = 400


class ArtifactRootLockedError(DocumentProcessingError):
    code = "storage_unavailable"
    status_code = 503


class ServiceNotReadyError(DocumentProcessingError):
    code = "worker_unavailable"
    status_code = 503


class QueueCapacityError(DocumentProcessingError):
    code = "queue_full"
    status_code = 429


class RunNotFoundError(DocumentProcessingError):
    code = "run_not_found"
    status_code = 404


class InvalidRunOperationError(DocumentProcessingError):
    code = "invalid_run_state"
    status_code = 409


class UploadTooLargeError(DocumentProcessingError):
    code = "upload_too_large"
    status_code = 413


class UnsupportedMediaTypeError(DocumentProcessingError):
    code = "unsupported_media_type"
    status_code = 415


class InvalidPdfError(DocumentProcessingError):
    code = "invalid_pdf"
    status_code = 422


class EncryptedPdfError(DocumentProcessingError):
    code = "encrypted_pdf"
    status_code = 422


class PageLimitError(DocumentProcessingError):
    code = "page_limit_exceeded"
    status_code = 422


class RenderSizeLimitError(DocumentProcessingError):
    code = "render_size_limit_exceeded"
    status_code = 422


class ManifestNotReadyError(DocumentProcessingError):
    code = "result_not_ready"
    status_code = 409
