"""Deterministic service-port fakes."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from document_processing.contracts import RunStatus
from document_processing.processing.cancellation import ProcessingCancelled
from document_processing.service.models import (
    FailureView,
    PagePage,
    ProcessingOutcome,
    ProgressView,
    RecoveryItem,
    RunView,
    Submission,
)
from document_processing.service.protocols import Cancellation


def run_view(
    run_id: str,
    document_id: str,
    status: RunStatus = RunStatus.NOT_STARTED,
    *,
    source: Path | None = None,
    failure: FailureView | None = None,
    cancel_requested: bool = False,
) -> RunView:
    now = datetime.now(UTC)
    return RunView(
        run_id=run_id,
        document_id=document_id,
        status=status,
        phase=status.value,
        progress=ProgressView(),
        resumable=status is RunStatus.FAILED,
        cancel_requested=cancel_requested,
        created_at=now,
        updated_at=now,
        failure=failure,
    )


class FakeRepository:
    def __init__(self) -> None:
        self.runs: dict[str, RunView] = {}
        self.sources: dict[str, Path] = {}
        self.submissions: list[Submission] = []
        self.recoveries: list[RecoveryItem] = []

    async def register_submission(self, submission: Submission) -> RunView:
        view = run_view(submission.run_id, submission.document_id)
        self.runs[submission.run_id] = view
        self.sources[submission.run_id] = submission.source_path
        self.submissions.append(submission)
        return view

    async def get_run(self, run_id: str) -> RunView:
        return self.runs[run_id]

    async def list_pages(self, run_id: str, *, offset: int, limit: int) -> PagePage:
        self.runs[run_id]
        return PagePage(items=(), offset=offset, limit=limit, total=0)

    async def request_cancel(self, run_id: str) -> RunView:
        current = self.runs[run_id]
        if current.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
            return current
        failure = FailureView("cancelled", "cancelled", "Cancelled.", False)
        if current.status in {RunStatus.NOT_STARTED, RunStatus.RESUMING}:
            updated = replace(
                current,
                status=RunStatus.FAILED,
                phase="failed",
                resumable=True,
                cancel_requested=True,
                failure=failure,
            )
        else:
            updated = replace(current, cancel_requested=True)
        self.runs[run_id] = updated
        return updated

    async def prepare_resume(self, run_id: str) -> tuple[RunView, Path]:
        current = self.runs[run_id]
        updated = replace(
            current,
            status=RunStatus.RESUMING,
            phase="resuming",
            resumable=False,
            cancel_requested=False,
            failure=None,
        )
        self.runs[run_id] = updated
        return updated, self.sources[run_id]

    async def fail_run(self, run_id: str, failure: FailureView) -> RunView:
        current = self.runs[run_id]
        updated = replace(
            current,
            status=RunStatus.FAILED,
            phase="failed",
            resumable=True,
            failure=failure,
        )
        self.runs[run_id] = updated
        return updated

    async def get_manifest(self, run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, "status": "completed"}

    async def recover_startup(self, configuration_fingerprint: str) -> list[RecoveryItem]:
        assert len(configuration_fingerprint) == 64
        return self.recoveries

    async def drain_offloads(self) -> None:
        return None

    def set_running(self, run_id: str) -> None:
        self.runs[run_id] = replace(self.runs[run_id], status=RunStatus.RUNNING, phase="running")

    def set_completed(self, run_id: str) -> None:
        current = self.runs[run_id]
        self.runs[run_id] = replace(
            current,
            status=RunStatus.COMPLETED,
            phase="completed",
            resumable=False,
            failure=None,
        )


class FakeProcessor:
    def __init__(self, repository: FakeRepository, *, blocked: bool = False) -> None:
        self.repository = repository
        self.calls: list[tuple[str, bool]] = []
        self.started: asyncio.Queue[str] = asyncio.Queue()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def process_run(
        self,
        *,
        run_id: str,
        source_path: Path,
        cancellation: Cancellation,
        resume: bool,
    ) -> ProcessingOutcome:
        assert source_path == self.repository.sources[run_id]
        self.calls.append((run_id, resume))
        self.repository.set_running(run_id)
        await self.started.put(run_id)
        while not self.release.is_set() and not cancellation.is_cancelled:
            await asyncio.sleep(0)
        if cancellation.is_cancelled:
            raise ProcessingCancelled("cancelled")
        self.repository.set_completed(run_id)
        return ProcessingOutcome(run_id, RunStatus.COMPLETED)

    async def drain_offloads(self) -> None:
        return None


class AcceptingValidator:
    def validate(self, path: Path) -> int:
        assert path.read_bytes().startswith(b"%PDF-")
        return 1
