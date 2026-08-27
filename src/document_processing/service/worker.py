"""The embedded one-at-a-time FIFO worker."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass

from document_processing.processing.cancellation import ProcessingCancelled
from document_processing.service.models import (
    TERMINAL_RUN_STATUSES,
    FailureView,
    RecoveryItem,
    WorkItem,
)
from document_processing.service.protocols import Cancellation, RunProcessor, RunServiceRepository


@dataclass(frozen=True, slots=True)
class _QueuedWork:
    item: WorkItem
    generation: int


class SingleRunWorker:
    """Process durable work serially while bounding in-memory admission."""

    def __init__(
        self,
        *,
        repository: RunServiceRepository,
        processor: RunProcessor,
        cancellation_factory: Callable[[], Cancellation],
        capacity: int,
    ) -> None:
        self._repository = repository
        self._processor = processor
        self._cancellation_factory = cancellation_factory
        self._queue: asyncio.Queue[_QueuedWork | None] = asyncio.Queue(maxsize=capacity)
        self._backlog: deque[_QueuedWork] = deque()
        self._enqueued: set[str] = set()
        self._reserved_ids: set[str] = set()
        self._reserved_generations: dict[str, int] = {}
        self._anonymous_reservations = 0
        self._generations: dict[str, int] = {}
        self._events: dict[tuple[str, int], asyncio.Event] = {}
        self._released: dict[str, asyncio.Event] = {}
        self._active_run_id: str | None = None
        self._active_token: Cancellation | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closing = False

    @property
    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, recoveries: Sequence[RecoveryItem]) -> None:
        if self._task is not None:
            return
        async with self._lock:
            for recovery in recoveries:
                item = WorkItem(recovery.run_id, recovery.source_path, recovery.resume)
                if item.run_id in self._enqueued:
                    raise RuntimeError(f"startup recovery returned duplicate run {item.run_id}")
                queued = self._new_generation_locked(item)
                self._backlog.append(queued)
                self._enqueued.add(item.run_id)
                self._released.setdefault(item.run_id, asyncio.Event()).clear()
            self._fill_from_backlog_locked()
        self._task = asyncio.create_task(self._run(), name="document-run-worker")

    async def stop(self, grace_seconds: float) -> None:
        self._closing = True
        task = self._task
        interrupted = False
        try:
            if task is None or task.done():
                return
            if self._active_run_id is None and self._queue.empty():
                self._queue.put_nowait(None)
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=grace_seconds)
            except TimeoutError:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        except asyncio.CancelledError:
            interrupted = True
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        finally:
            await self._release_pending()
            try:
                await self._processor.drain_offloads()
            except asyncio.CancelledError:
                interrupted = True
        if interrupted:
            raise asyncio.CancelledError

    async def reserve(self, run_id: str | None = None) -> None:
        """Reserve bounded capacity before a durable state change."""

        async with self._lock:
            if run_id is not None and (run_id in self._enqueued or run_id in self._reserved_ids):
                raise RuntimeError(f"run {run_id} is already queued")
            if not self._has_capacity_locked():
                raise OverflowError("processing queue is full")
            if run_id is None:
                self._anonymous_reservations += 1
            else:
                self._reserved_ids.add(run_id)
                self._reserved_generations[run_id] = self._advance_generation_locked(run_id)

    async def release_reservation(self, run_id: str | None = None) -> None:
        async with self._lock:
            if run_id is None:
                if self._anonymous_reservations:
                    self._anonymous_reservations -= 1
            else:
                self._reserved_ids.discard(run_id)
                self._reserved_generations.pop(run_id, None)

    async def enqueue_reserved(self, item: WorkItem, *, anonymous: bool = False) -> None:
        async with self._lock:
            if anonymous:
                if self._anonymous_reservations < 1:
                    raise RuntimeError("submission queue capacity was not reserved")
                self._anonymous_reservations -= 1
            elif item.run_id not in self._reserved_ids:
                raise RuntimeError(f"run {item.run_id} queue capacity was not reserved")
            else:
                self._reserved_ids.remove(item.run_id)
            if anonymous:
                queued = self._new_generation_locked(item)
            else:
                generation = self._reserved_generations.pop(item.run_id)
                self._events[(item.run_id, generation)].clear()
                queued = _QueuedWork(item=item, generation=generation)
            self._queue.put_nowait(queued)
            self._enqueued.add(item.run_id)
            self._released.setdefault(item.run_id, asyncio.Event()).clear()

    def cancel_active(self, run_id: str) -> None:
        if self._active_run_id == run_id and self._active_token is not None:
            self._active_token.cancel()

    def signal_terminal(self, run_id: str, generation: int | None = None) -> None:
        selected = self._generations.get(run_id, 0) if generation is None else generation
        self._events.setdefault((run_id, selected), asyncio.Event()).set()

    def terminal_event(self, run_id: str) -> tuple[int, asyncio.Event]:
        generation = self._generations.get(run_id, 0)
        return generation, self._events.setdefault((run_id, generation), asyncio.Event())

    async def wait_until_released(self, run_id: str) -> None:
        """Wait until an older queued/active item no longer owns this run."""

        if run_id not in self._enqueued and self._active_run_id != run_id:
            return
        event = self._released.setdefault(run_id, asyncio.Event())
        await event.wait()

    def _has_capacity_locked(self) -> bool:
        occupied = (
            self._queue.qsize()
            + len(self._backlog)
            + len(self._reserved_ids)
            + self._anonymous_reservations
        )
        return occupied < self._queue.maxsize

    def _fill_from_backlog_locked(self) -> None:
        while self._backlog and not self._queue.full():
            self._queue.put_nowait(self._backlog.popleft())

    def _new_generation_locked(self, item: WorkItem) -> _QueuedWork:
        generation = self._advance_generation_locked(item.run_id)
        return _QueuedWork(item=item, generation=generation)

    def _advance_generation_locked(self, run_id: str) -> int:
        generation = self._generations.get(run_id, 0) + 1
        self._generations[run_id] = generation
        self._events[(run_id, generation)] = asyncio.Event()
        return generation

    async def _run(self) -> None:
        while True:
            queued = await self._queue.get()
            if queued is None:
                self._queue.task_done()
                break
            if self._should_stop():
                self._release_queued(queued)
                self._queue.task_done()
                break
            await self._process_item(queued)
            self._queue.task_done()
            if self._should_stop():
                break
            async with self._lock:
                self._fill_from_backlog_locked()

    def _should_stop(self) -> bool:
        return self._closing

    async def _process_item(self, queued: _QueuedWork) -> None:
        item = queued.item
        token = self._cancellation_factory()
        self._active_run_id = item.run_id
        self._active_token = token
        try:
            existing = await self._repository.get_run(item.run_id)
            if existing.status in TERMINAL_RUN_STATUSES:
                return
            await self._processor.process_run(
                run_id=item.run_id,
                source_path=item.source_path,
                cancellation=token,
                resume=item.resume,
            )
            await self._ensure_terminal(item.run_id, token)
        except asyncio.CancelledError:
            raise
        except ProcessingCancelled:
            await self._mark_cancelled(item.run_id)
        except Exception:
            await self._mark_failed_unless_terminal(item.run_id, token)
        finally:
            self._enqueued.discard(item.run_id)
            self._active_run_id = None
            self._active_token = None
            self._released.setdefault(item.run_id, asyncio.Event()).set()
            with suppress(Exception):
                view = await self._repository.get_run(item.run_id)
                if view.status in TERMINAL_RUN_STATUSES:
                    self.signal_terminal(item.run_id, queued.generation)

    async def _release_pending(self) -> None:
        """Release every abandoned queue ownership after the worker has stopped."""

        async with self._lock:
            while self._backlog:
                self._release_queued(self._backlog.popleft())
            while True:
                try:
                    queued = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if queued is not None:
                    self._release_queued(queued)
                self._queue.task_done()
            self._reserved_ids.clear()
            self._reserved_generations.clear()
            self._anonymous_reservations = 0

    def _release_queued(self, queued: _QueuedWork) -> None:
        run_id = queued.item.run_id
        self._enqueued.discard(run_id)
        self._released.setdefault(run_id, asyncio.Event()).set()

    async def _ensure_terminal(self, run_id: str, token: Cancellation) -> None:
        view = await self._repository.get_run(run_id)
        if view.status in TERMINAL_RUN_STATUSES:
            return
        if token.is_cancelled or view.cancel_requested:
            await self._mark_cancelled(run_id)
        else:
            await self._repository.fail_run(
                run_id,
                FailureView(
                    code="processor_incomplete",
                    category="internal",
                    message="The processor stopped without producing a terminal result.",
                    retryable=False,
                ),
            )

    async def _mark_failed_unless_terminal(self, run_id: str, token: Cancellation) -> None:
        with suppress(Exception):
            view = await self._repository.get_run(run_id)
            if view.status in TERMINAL_RUN_STATUSES:
                return
            if token.is_cancelled or view.cancel_requested:
                await self._mark_cancelled(run_id)
            else:
                await self._repository.fail_run(
                    run_id,
                    FailureView(
                        code="processing_failed",
                        category="internal",
                        message="Document processing failed.",
                        retryable=False,
                    ),
                )

    async def _mark_cancelled(self, run_id: str) -> None:
        await self._repository.fail_run(
            run_id,
            FailureView(
                code="cancelled",
                category="cancelled",
                message="Document processing was cancelled.",
                retryable=False,
            ),
        )
