"""Cancellation-safe cleanup for partially admitted uploads."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

from document_processing.processing.offload import BlockingCallTracker
from document_processing.service.models import FailureView
from document_processing.service.protocols import RunServiceRepository
from document_processing.service.uploads import UploadPreserver


async def cleanup_submission(
    *,
    run_id: str,
    registration_started: bool,
    artifact_root: Path,
    repository: RunServiceRepository,
    offloads: BlockingCallTracker,
    uploads: UploadPreserver,
) -> None:
    """Settle detached work, then fail or quarantine the partial submission."""

    with suppress(asyncio.CancelledError):
        await repository.drain_offloads()
    with suppress(asyncio.CancelledError):
        await offloads.drain()
    run_directory = artifact_root / "runs" / run_id
    if registration_started:
        with suppress(Exception, asyncio.CancelledError):
            await repository.fail_run(
                run_id,
                FailureView(
                    code="submission_abandoned",
                    category="internal",
                    message="The lifecycle operation did not finish.",
                    retryable=False,
                ),
            )
        return
    if not (run_directory.exists() or run_directory.is_symlink()):
        return
    try:
        await offloads.run(uploads.quarantine, run_id)
    except Exception:
        return
    except asyncio.CancelledError:
        with suppress(asyncio.CancelledError):
            await offloads.drain()
