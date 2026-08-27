"""Cooperative cancellation primitives for document processing."""

from __future__ import annotations

import asyncio
from threading import Event, Lock


class ProcessingCancelled(Exception):
    """Raised when cancellation is observed at a safe processing boundary."""


class CancellationToken:
    """A small thread-safe token that can interrupt asynchronous retry waits."""

    def __init__(self) -> None:
        self._event = Event()
        self._lock = Lock()
        self._reason: str | None = None

    def cancel(self, reason: str = "cancelled") -> None:
        """Idempotently request cancellation while preserving the first reason."""

        with self._lock:
            if self._event.is_set():
                return
            self._reason = reason
            self._event.set()

    @property
    def is_cancelled(self) -> bool:
        """Whether cancellation has been requested."""

        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        """The first cancellation reason, if one was supplied."""

        with self._lock:
            return self._reason

    def raise_if_cancelled(self) -> None:
        """Raise at a cooperative boundary when cancellation was requested."""

        if self.is_cancelled:
            raise ProcessingCancelled(self.reason or "cancelled")

    async def wait_cancelled(self, timeout: float | None = None) -> bool:
        """Wait without blocking the event loop; return whether cancellation won."""

        if self.is_cancelled:
            return True
        return await asyncio.to_thread(self._event.wait, timeout)
