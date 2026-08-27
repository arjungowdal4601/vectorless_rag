"""Application-owned transient retry policy with interruptible backoff."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from document_processing.processing.cancellation import ProcessingCancelled

Clock = Callable[[], float]
RandomSource = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]
CancellationProbe = Callable[[], bool]
AsyncCancellationProbe = Callable[[], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff; max_attempts includes the first request."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    jitter_ratio: float = 0.25
    cancellation_poll_seconds: float = 0.1

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.max_attempts > 3:
            raise ValueError("max_attempts cannot exceed three total attempts")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays must be non-negative")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between zero and one")
        if self.cancellation_poll_seconds <= 0:
            raise ValueError("cancellation_poll_seconds must be positive")

    def delay_after(self, failed_attempt: int, random_value: float) -> float:
        """Calculate a jittered delay after the numbered failed attempt."""

        if failed_attempt < 1:
            raise ValueError("failed_attempt must be positive")
        if not 0 <= random_value <= 1:
            raise ValueError("random_value must be between zero and one")
        base = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** (failed_attempt - 1)),
        )
        factor = 1 - self.jitter_ratio + 2 * self.jitter_ratio * random_value
        return float(min(self.max_delay_seconds, base * factor))


async def interruptible_backoff(
    delay_seconds: float,
    *,
    cancelled: CancellationProbe,
    clock: Clock = time.monotonic,
    sleep: Sleeper = asyncio.sleep,
    poll_seconds: float = 0.1,
) -> None:
    """Sleep in bounded slices and observe cancellation throughout the wait."""

    deadline = clock() + max(0.0, delay_seconds)
    while True:
        if cancelled():
            raise ProcessingCancelled("cancelled during retry backoff")
        remaining = deadline - clock()
        if remaining <= 0:
            return
        await sleep(min(remaining, poll_seconds))


async def interruptible_async_backoff(
    delay_seconds: float,
    *,
    cancelled: AsyncCancellationProbe,
    clock: Clock = time.monotonic,
    sleep: Sleeper = asyncio.sleep,
    poll_seconds: float = 0.1,
) -> None:
    """Interrupt backoff while offloading any durable cancellation read."""

    deadline = clock() + max(0.0, delay_seconds)
    while True:
        if await cancelled():
            raise ProcessingCancelled("cancelled during retry backoff")
        remaining = deadline - clock()
        if remaining <= 0:
            return
        await sleep(min(remaining, poll_seconds))
