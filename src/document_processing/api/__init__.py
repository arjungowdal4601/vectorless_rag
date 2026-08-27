"""FastAPI presentation layer for local document-processing runs."""

from document_processing.api.app import create_app
from document_processing.api.contracts import (
    LocalRunServiceProtocol,
    PagePage,
    ServiceError,
)

__all__ = [
    "LocalRunServiceProtocol",
    "PagePage",
    "ServiceError",
    "create_app",
]
