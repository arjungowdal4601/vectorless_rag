"""Shared lifecycle helpers for orchestration entry points and page runners."""

from __future__ import annotations

from dataclasses import dataclass

from document_processing.processing.cancellation import (
    CancellationToken,
    ProcessingCancelled,
)
from document_processing.processing.errors import ProcessingFailure
from document_processing.processing.interfaces import ProcessingRepository, RunHandle
from document_processing.processing.models import ProcessingResult
from document_processing.processing.offload import BlockingCallTracker


@dataclass(slots=True)
class CancellationScope:
    """Combine process-local and durable cancellation sources."""

    token: CancellationToken
    repository: ProcessingRepository
    run_id: str
    offloads: BlockingCallTracker

    def cancelled(self) -> bool:
        return self.token.is_cancelled or self.repository.is_cancel_requested(self.run_id)

    async def is_cancelled(self) -> bool:
        """Check durable cancellation without blocking the event loop."""

        if self.token.is_cancelled:
            return True
        return await self.offloads.run(
            self.repository.is_cancel_requested,
            self.run_id,
        )

    async def check(self) -> None:
        if await self.is_cancelled():
            raise ProcessingCancelled(self.token.reason or "cancelled")


async def failed_result(
    repository: ProcessingRepository,
    run: RunHandle,
    *,
    document_id: str | None,
    completed: int,
    total: int,
    failure: ProcessingFailure,
    offloads: BlockingCallTracker,
) -> ProcessingResult:
    """Persist and return one sanitized failed outcome."""

    await offloads.run(repository.fail_run, run.run_id, failure)
    try:
        authoritative = await offloads.run(repository.load_run, run.run_id)
    except Exception:
        authoritative = run
    if authoritative.status == "failed" and authoritative.failure is not None:
        failure = authoritative.failure
    document_id = authoritative.document_id or document_id
    if authoritative.completed_pages is not None:
        completed = authoritative.completed_pages
    if authoritative.total_pages is not None:
        total = authoritative.total_pages
    return ProcessingResult(
        run_id=run.run_id,
        document_id=document_id,
        status="failed",
        completed_pages=completed,
        total_pages=total,
        artifact_root=run.run_directory,
        failure=failure,
    )
