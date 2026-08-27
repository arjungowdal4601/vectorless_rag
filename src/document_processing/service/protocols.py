"""Dependency-inversion boundaries for the embedded service."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from document_processing.service.models import (
    FailureView,
    PagePage,
    RecoveryItem,
    RunView,
    Submission,
)


@runtime_checkable
class Cancellation(Protocol):
    @property
    def is_cancelled(self) -> bool: ...

    def cancel(self, reason: str = "cancelled") -> None: ...

    def raise_if_cancelled(self) -> None: ...

    async def wait_cancelled(self, timeout: float | None = None) -> bool: ...


@runtime_checkable
class RunProcessor(Protocol):
    async def process_run(
        self,
        *,
        run_id: str,
        source_path: Path,
        cancellation: Cancellation,
        resume: bool,
    ) -> object:
        """Process or resume one durable run, updating its repository state."""

    async def drain_offloads(self) -> None:
        """Wait for cancelled blocking work to stop touching durable storage."""


@runtime_checkable
class RunServiceRepository(Protocol):
    async def register_submission(self, submission: Submission) -> RunView:
        """Atomically register an already-durable source as a not-started run."""

    async def get_run(self, run_id: str) -> RunView: ...

    async def list_pages(self, run_id: str, *, offset: int, limit: int) -> PagePage: ...

    async def request_cancel(self, run_id: str) -> RunView:
        """Persist a cooperative request, failing queued runs as cancelled."""

    async def prepare_resume(self, run_id: str) -> tuple[RunView, Path]:
        """Audit a failed run and atomically transition it to resuming."""

    async def fail_run(self, run_id: str, failure: FailureView) -> RunView: ...

    async def get_manifest(self, run_id: str) -> Mapping[str, Any]: ...

    async def recover_startup(self, configuration_fingerprint: str) -> Sequence[RecoveryItem]:
        """Audit interrupted work and return it in durable FIFO order."""

    async def drain_offloads(self) -> None:
        """Wait for detached blocking journal calls before releasing storage ownership."""
