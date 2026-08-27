"""Cancellation-safe cleanup for an abandoned resume claim."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from document_processing.contracts import RunStatus
from document_processing.service.models import FailureView
from document_processing.service.protocols import RunServiceRepository


async def rollback_abandoned_resume(
    repository: RunServiceRepository,
    run_id: str,
) -> None:
    """Fail only a resume claim left active after the caller was interrupted."""

    with suppress(asyncio.CancelledError):
        await repository.drain_offloads()
    with suppress(Exception, asyncio.CancelledError):
        view = await repository.get_run(run_id)
        if view.status is RunStatus.RESUMING:
            await repository.fail_run(
                run_id,
                FailureView(
                    code="resume_abandoned",
                    category="internal",
                    message="The lifecycle operation did not finish.",
                    retryable=False,
                ),
            )
