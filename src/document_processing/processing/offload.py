"""Tracked execution of blocking intake and persistence calls."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Any


class BlockingCallTracker:
    """Shield thread work from task cancellation and expose a shutdown drain."""

    def __init__(self) -> None:
        self._active: set[asyncio.Future[Any]] = set()

    @property
    def active_count(self) -> int:
        """Number of submitted calls whose worker threads have not finished."""

        return len(self._active)

    async def run[**P, T](
        self,
        function: Callable[P, T],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Run one call in the default executor and retain it if awaiting is cancelled."""

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, partial(function, *args, **kwargs))
        self._active.add(future)
        future.add_done_callback(self._settled)
        return await asyncio.shield(future)

    async def drain(self) -> None:
        """Wait until all submitted calls leave their threads, deferring cancellation."""

        interrupted = False
        current = asyncio.current_task()
        while self._active:
            snapshot = tuple(self._active)
            pending = asyncio.gather(*snapshot, return_exceptions=True)
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    interrupted = True
                    if current is not None:
                        current.uncancel()
            await pending
        if interrupted:
            raise asyncio.CancelledError

    def _settled(self, future: asyncio.Future[Any]) -> None:
        self._active.discard(future)
        if not future.cancelled():
            future.exception()
