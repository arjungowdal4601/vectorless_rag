"""Concrete adapters joining processing, storage, and the local service."""

from .processing_repository import DurableProcessingRepository
from .service_repository import DurableRunServiceRepository

__all__ = ["DurableProcessingRepository", "DurableRunServiceRepository"]
