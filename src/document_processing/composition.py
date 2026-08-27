"""Production dependency composition for the reusable library and local API."""

from __future__ import annotations

from pathlib import Path

from document_processing.analysis import build_deep_agent_analyzer
from document_processing.config import Settings
from document_processing.pdf import PdfIntakeService, PdfRenderConfig
from document_processing.processing import (
    Analyzer,
    CancellationToken,
    DocumentProcessor,
    RetryPolicy,
)
from document_processing.processing.offload import BlockingCallTracker
from document_processing.runtime import (
    DurableProcessingRepository,
    DurableRunServiceRepository,
)
from document_processing.service import LocalRunService
from document_processing.service.protocols import Cancellation
from document_processing.storage import RunRepository


class _ServiceProcessor:
    """Widen the worker port while retaining the concrete cancellation token."""

    def __init__(self, processor: DocumentProcessor) -> None:
        self._processor = processor

    async def process_run(
        self,
        *,
        run_id: str,
        source_path: Path,
        cancellation: Cancellation,
        resume: bool,
    ) -> object:
        if not isinstance(cancellation, CancellationToken):
            raise TypeError("the local worker requires a CancellationToken")
        return await self._processor.process_run(
            run_id=run_id,
            source_path=source_path,
            cancellation=cancellation,
            resume=resume,
        )

    async def drain_offloads(self) -> None:
        await self._processor.drain_offloads()


def create_processor(
    settings: Settings,
    *,
    analyzer: Analyzer | None = None,
    storage: RunRepository | None = None,
    offload_tracker: BlockingCallTracker | None = None,
) -> DocumentProcessor:
    """Compose the reusable processor, optionally injecting offline collaborators."""

    durable_storage = storage or RunRepository(settings.artifact_root)
    durable_storage.initialize()
    return _compose_processor(
        settings,
        storage=durable_storage,
        analyzer=analyzer,
        offload_tracker=offload_tracker,
    )


def _compose_processor(
    settings: Settings,
    *,
    storage: RunRepository,
    analyzer: Analyzer | None,
    offload_tracker: BlockingCallTracker | None,
) -> DocumentProcessor:
    """Wire processing collaborators around storage initialized by their owner."""

    page_analyzer = analyzer or build_deep_agent_analyzer(
        model_name=settings.model,
        reasoning_effort=settings.reasoning_effort,
        timeout_seconds=settings.model_timeout_seconds,
    )
    intake = PdfIntakeService(
        PdfRenderConfig(
            dpi=settings.render_dpi,
            max_upload_bytes=settings.max_upload_bytes,
            max_pages=settings.max_pages,
            max_page_pixels=int(settings.max_rendered_megapixels * 1_000_000),
        )
    )
    repository = DurableProcessingRepository(
        storage,
        settings.fingerprint_payload(),
        settings.fingerprint,
    )
    return DocumentProcessor(
        analyzer=page_analyzer,
        pdf_intake=intake,
        repository=repository,
        retry_policy=RetryPolicy(max_attempts=settings.max_model_attempts),
        offload_tracker=offload_tracker,
    )


def create_service(settings: Settings) -> LocalRunService:
    """Compose one loopback local service around a shared durable repository."""

    storage = RunRepository(settings.artifact_root)
    offloads = BlockingCallTracker()
    processor = _compose_processor(
        settings,
        storage=storage,
        analyzer=None,
        offload_tracker=offloads,
    )
    repository = DurableRunServiceRepository(
        storage,
        settings.fingerprint,
        settings.max_model_attempts,
        offload_tracker=offloads,
    )
    return LocalRunService(
        settings=settings,
        repository=repository,
        processor=_ServiceProcessor(processor),
        offload_tracker=offloads,
    )
