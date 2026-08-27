"""Event-loop responsiveness tests for blocking intake and persistence ports."""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event

import pytest

from document_processing.processing import (
    BlockingCallTracker,
    CancellationToken,
    DocumentProcessor,
    FailureCode,
)
from tests.processing.factories import analysis_result
from tests.processing.fakes import (
    DOCUMENT_ID,
    FakePdfIntake,
    FakePreparedPdf,
    FakeRepository,
    FakeStoredPdf,
    QueueAnalyzer,
    prepared_pdf,
)


class BlockingIntake(FakePdfIntake):
    def __init__(self, prepared: FakePreparedPdf) -> None:
        super().__init__(prepared)
        self.started = Event()
        self.release = Event()

    def preserve_path(self, source_path: Path, run_dir: Path) -> FakeStoredPdf:
        self.preserve_calls.append((source_path, run_dir))
        self.started.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("test did not release intake")
        return FakeStoredPdf(self.prepared.document_id, self.prepared.source_path)


async def wait_for_thread_event(event: Event) -> None:
    while not event.is_set():
        await asyncio.sleep(0.001)


async def heartbeat_for(seconds: float) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    beats = 0
    while loop.time() < deadline:
        beats += 1
        await asyncio.sleep(0.001)
    return beats


def test_slow_pdf_intake_does_not_block_heartbeat_or_cancellation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        prepared = prepared_pdf(tmp_path, 1)
        repository = FakeRepository(tmp_path, prepared)
        intake = BlockingIntake(prepared)
        analyzer = QueueAnalyzer([])
        processor = DocumentProcessor(
            analyzer=analyzer,
            pdf_intake=intake,
            repository=repository,
        )
        token = CancellationToken()
        task = asyncio.create_task(processor.process_pdf(tmp_path / "input.pdf", token))
        await wait_for_thread_event(intake.started)

        beats = await heartbeat_for(0.03)
        token.cancel()
        intake.release.set()
        result = await asyncio.wait_for(task, timeout=2)

        assert beats >= 5
        assert result.status == "failed"
        assert result.failure is not None
        assert result.failure.code is FailureCode.CANCELLED
        assert analyzer.calls == []
        assert repository.commits == []

    asyncio.run(scenario())


def test_slow_page_commit_does_not_block_event_loop(tmp_path: Path) -> None:
    async def scenario() -> None:
        prepared = prepared_pdf(tmp_path, 1)
        repository = FakeRepository(tmp_path, prepared)
        commit_started = Event()
        release_commit = Event()

        def block_commit(_: object) -> None:
            commit_started.set()
            if not release_commit.wait(timeout=2):
                raise TimeoutError("test did not release commit")

        repository.commit_callback = block_commit
        processor = DocumentProcessor(
            analyzer=QueueAnalyzer([analysis_result(1)]),
            pdf_intake=FakePdfIntake(prepared),
            repository=repository,
        )
        task = asyncio.create_task(processor.process_pdf(tmp_path / "input.pdf"))
        await wait_for_thread_event(commit_started)

        beats = await heartbeat_for(0.03)
        release_commit.set()
        result = await asyncio.wait_for(task, timeout=2)

        assert beats >= 5
        assert result.status == "completed"

    asyncio.run(scenario())


def test_forced_shutdown_drains_detached_intake_without_marking_failed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        prepared = prepared_pdf(tmp_path, 1)
        repository = FakeRepository(tmp_path, prepared)
        intake = BlockingIntake(prepared)
        processor = DocumentProcessor(
            analyzer=QueueAnalyzer([]),
            pdf_intake=intake,
            repository=repository,
        )
        processing = asyncio.create_task(processor.process_pdf(tmp_path / "input.pdf"))
        await wait_for_thread_event(intake.started)

        processing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await processing

        draining = asyncio.create_task(processor.drain_offloads())
        beats = await heartbeat_for(0.03)
        assert beats >= 5
        assert not draining.done()
        assert repository.failures == []

        intake.release.set()
        await asyncio.wait_for(draining, timeout=2)
        assert repository.failures == []
        assert repository.handle.document_id == DOCUMENT_ID

    asyncio.run(scenario())


def test_drain_defers_its_own_cancellation_until_blocking_call_finishes() -> None:
    async def scenario() -> None:
        tracker = BlockingCallTracker()
        started = Event()
        release = Event()

        def blocking_call() -> None:
            started.set()
            if not release.wait(timeout=2):
                raise TimeoutError("test did not release blocking call")

        caller = asyncio.create_task(tracker.run(blocking_call))
        await wait_for_thread_event(started)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        draining = asyncio.create_task(tracker.drain())
        await asyncio.sleep(0)
        draining.cancel()
        await asyncio.sleep(0.01)
        assert not draining.done()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(draining, timeout=2)
        assert tracker.active_count == 0

    asyncio.run(scenario())
