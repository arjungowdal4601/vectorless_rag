"""Public library surface for multimodal PDF document processing."""

from document_processing.composition import create_processor, create_service
from document_processing.config import Settings
from document_processing.processing import DocumentProcessor, ProcessingResult
from document_processing.service import LocalRunService

__all__ = [
    "DocumentProcessor",
    "LocalRunService",
    "ProcessingResult",
    "Settings",
    "create_processor",
    "create_service",
]
__version__ = "0.1.0"
