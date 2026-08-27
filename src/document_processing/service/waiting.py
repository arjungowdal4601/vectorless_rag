"""Generation-safe durable run waiting."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from document_processing.service.models import TERMINAL_RUN_STATUSES, RunView
from document_processing.service.protocols import RunServiceRepository
from document_processing.service.worker import SingleRunWorker


async def wait_for_terminal(
    *,
    repository: RunServiceRepository,
    worker: SingleRunWorker,
    shutdown_started: asyncio.Event,
    require_ready: Callable[[], None],
    run_id: str,
    timeout: float | None,
) -> RunView:
    """Wait for the current work generation and always recheck durable state."""

    async def observe() -> RunView:
        while True:
            require_ready()
            view = await repository.get_run(run_id)
            if view.status in TERMINAL_RUN_STATUSES:
                return view
            _generation, event = worker.terminal_event(run_id)
            view = await repository.get_run(run_id)
            if view.status in TERMINAL_RUN_STATUSES:
                return view
            require_ready()
            terminal = asyncio.create_task(event.wait())
            shutdown = asyncio.create_task(shutdown_started.wait())
            try:
                await asyncio.wait((terminal, shutdown), return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pending in (terminal, shutdown):
                    if not pending.done():
                        pending.cancel()
                await asyncio.gather(terminal, shutdown, return_exceptions=True)

    if timeout is None:
        return await observe()
    async with asyncio.timeout(timeout):
        return await observe()
