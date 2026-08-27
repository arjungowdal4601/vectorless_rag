"""Cancellation-safe service shutdown waiting."""

from __future__ import annotations

import asyncio


async def await_shutdown(task: asyncio.Task[None]) -> None:
    """Defer caller cancellation until the storage-owning shutdown has finished."""

    interrupted = False
    current = asyncio.current_task()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
            if current is not None:
                current.uncancel()
    task.result()
    if interrupted:
        raise asyncio.CancelledError
