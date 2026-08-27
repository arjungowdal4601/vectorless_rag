"""Durable local application service for document-processing runs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from document_processing.config import Settings
from document_processing.contracts import RunStatus
from document_processing.errors import (
    ArtifactRootLockedError,
    InvalidPdfError,
    InvalidRequestError,
    ManifestNotReadyError,
    QueueCapacityError,
    ServiceNotReadyError,
)
from document_processing.processing.cancellation import CancellationToken
from document_processing.processing.offload import BlockingCallTracker
from document_processing.service.intake_cleanup import cleanup_submission
from document_processing.service.lock import ArtifactRootLock
from document_processing.service.models import (
    TERMINAL_RUN_STATUSES,
    HealthView,
    PagePage,
    ReadyView,
    RunView,
    Submission,
    WorkItem,
)
from document_processing.service.protocols import Cancellation, RunProcessor, RunServiceRepository
from document_processing.service.resume_cleanup import rollback_abandoned_resume
from document_processing.service.shutdown import await_shutdown
from document_processing.service.uploads import UploadPreserver, file_chunks
from document_processing.service.validation import (
    PdfiumSubmissionValidator,
    SubmissionValidator,
    validate_run_id,
    validate_source_path,
    validate_submission,
)
from document_processing.service.waiting import wait_for_terminal
from document_processing.service.worker import SingleRunWorker

CancellationFactory = Callable[[], Cancellation]


class LocalRunService:
    """Own durable admission, public queries, and one FIFO worker."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: RunServiceRepository,
        processor: RunProcessor,
        cancellation_factory: CancellationFactory = CancellationToken,
        artifact_lock: ArtifactRootLock | None = None,
        submission_validator: SubmissionValidator | None = None,
        offload_tracker: BlockingCallTracker | None = None,
    ) -> None:
        self.settings = settings
        self._repository = repository
        self._artifact_lock = artifact_lock or ArtifactRootLock(settings.artifact_root)
        self._uploads = UploadPreserver(settings.artifact_root, settings.max_upload_bytes)
        self._validator = submission_validator or PdfiumSubmissionValidator(
            max_pages=settings.max_pages,
            max_page_pixels=int(settings.max_rendered_megapixels * 1_000_000),
            dpi=settings.render_dpi,
        )
        self._offloads = offload_tracker or BlockingCallTracker()
        self._worker = SingleRunWorker(
            repository=repository,
            processor=processor,
            cancellation_factory=cancellation_factory,
            capacity=settings.worker_queue_capacity,
        )
        self._started = False
        self._closing = False
        self._closed = False
        self._active_mutations = 0
        self._admission_condition = asyncio.Condition()
        self._startup_task: asyncio.Task[None] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._shutdown_started = asyncio.Event()
        self._readiness_reason: str | None = "not_started"

    async def start(self) -> None:
        """Acquire ownership, audit durable work, and start one worker."""

        if self._closed or self._closing:
            raise ServiceNotReadyError("A closed local run service cannot be restarted.")
        if self._startup_task is None:
            self._startup_task = asyncio.create_task(
                self._start_owned(), name="document-service-startup"
            )
        try:
            await asyncio.shield(self._startup_task)
        except asyncio.CancelledError:
            shutdown = self._begin_shutdown()
            await await_shutdown(shutdown)
            raise

    async def _start_owned(self) -> None:
        self._started = True
        try:
            self._artifact_lock.acquire()
        except ArtifactRootLockedError:
            self._readiness_reason = "storage_unavailable"
            return
        try:
            recoveries = await self._repository.recover_startup(self.settings.fingerprint)
            for recovery in recoveries:
                validate_source_path(
                    self.settings.artifact_root, recovery.run_id, recovery.source_path
                )
            if self._closing:
                return
            await self._worker.start(recoveries)
            if not self._closing:
                self._readiness_reason = None
        except Exception:
            self._readiness_reason = "startup_recovery_failed"

    async def close(self) -> None:
        """Quiesce publishers before releasing exclusive artifact-root ownership."""

        if self._closed:
            return
        await await_shutdown(self._begin_shutdown())

    def _begin_shutdown(self) -> asyncio.Task[None]:
        self._closing = True
        self._shutdown_started.set()
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(
                self._shutdown(), name="document-service-shutdown"
            )
        return self._shutdown_task

    async def __aenter__(self) -> LocalRunService:
        await self.start()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def submit_pdf(
        self,
        path: str | Path | None = None,
        *,
        filename: str | None = None,
        content_type: str = "application/pdf",
        chunks: AsyncIterable[bytes] | None = None,
    ) -> RunView:
        """Durably validate, preserve, register, and enqueue one PDF."""

        async with self._admit_mutation():
            if (path is None) == (chunks is None):
                raise InvalidRequestError("Provide exactly one PDF path or byte stream.")
            if path is not None:
                source = Path(path)
                if not source.is_file():
                    raise InvalidPdfError("The supplied PDF path is not a regular file.")
                filename = filename or source.name
                chunks = file_chunks(source)
            assert chunks is not None
            safe_filename = validate_submission(filename, content_type)
            return await self._submit_admitted(
                filename=safe_filename,
                content_type=content_type,
                chunks=chunks,
            )

    async def get_run(self, run_id: str) -> RunView:
        self._require_ready()
        return await self._repository.get_run(validate_run_id(run_id))

    async def list_pages(self, run_id: str, *, offset: int = 0, limit: int = 100) -> PagePage:
        self._require_ready()
        if offset < 0 or not 1 <= limit <= self.settings.page_list_max_limit:
            raise InvalidRequestError("Page offset or limit is outside the configured bounds.")
        return await self._repository.list_pages(
            validate_run_id(run_id), offset=offset, limit=limit
        )

    async def cancel(self, run_id: str) -> RunView:
        async with self._admit_mutation():
            normalized = validate_run_id(run_id)
            view = await self._repository.request_cancel(normalized)
            if view.cancel_requested:
                self._worker.cancel_active(normalized)
            if view.status in TERMINAL_RUN_STATUSES:
                self._worker.signal_terminal(normalized)
            return view

    async def cancel_run(self, run_id: str) -> RunView:
        return await self.cancel(run_id)

    async def resume(self, run_id: str) -> RunView:
        self._require_ready()
        normalized = validate_run_id(run_id)
        await self._worker.wait_until_released(normalized)
        async with self._admit_mutation():
            try:
                await self._worker.reserve(normalized)
            except (OverflowError, RuntimeError) as exc:
                raise QueueCapacityError(
                    "The processing queue is full.", run_id=normalized
                ) from exc
            try:
                view, source_path = await self._repository.prepare_resume(normalized)
                validate_source_path(self.settings.artifact_root, normalized, source_path)
                await self._worker.enqueue_reserved(WorkItem(normalized, source_path, resume=True))
                return view
            except BaseException:
                await rollback_abandoned_resume(self._repository, normalized)
                await self._worker.release_reservation(normalized)
                raise

    async def resume_run(self, run_id: str) -> RunView:
        return await self.resume(run_id)

    async def wait(self, run_id: str, timeout: float | None = None) -> RunView:
        self._require_ready()
        normalized = validate_run_id(run_id)
        view = await self._repository.get_run(normalized)
        if view.status in TERMINAL_RUN_STATUSES:
            return view
        return await self._wait_for_terminal(normalized, timeout)

    async def get_manifest(self, run_id: str) -> Mapping[str, Any]:
        normalized = validate_run_id(run_id)
        view = await self.get_run(normalized)
        if view.status is not RunStatus.COMPLETED:
            raise ManifestNotReadyError(
                "The manifest is available only after processing completes.", run_id=normalized
            )
        return await self._repository.get_manifest(normalized)

    async def health(self) -> HealthView:
        return HealthView()

    async def readiness(self) -> ReadyView:
        reason = self._current_readiness_reason()
        return ReadyView(ready=reason is None, reason=reason)

    def _require_ready(self) -> None:
        reason = self._current_readiness_reason()
        if reason is not None:
            raise ServiceNotReadyError(f"The local worker is not ready ({reason}).")

    def _current_readiness_reason(self) -> str | None:
        if self._readiness_reason is not None:
            return self._readiness_reason
        if self._closing:
            return "shutting_down"
        if not self._artifact_lock.is_held:
            return "storage_unavailable"
        if not self._worker.is_alive:
            return "worker_stopped"
        return None

    async def _submit_admitted(
        self,
        *,
        filename: str,
        content_type: str,
        chunks: AsyncIterable[bytes],
    ) -> RunView:
        try:
            await self._worker.reserve()
        except OverflowError as exc:
            raise QueueCapacityError("The processing queue is full.") from exc
        run_id = str(uuid4())
        registration_started = False
        try:
            preserved = await self._uploads.preserve(run_id, chunks)
            await self._offloads.run(self._validator.validate, preserved.path)
            submission = Submission(
                run_id=run_id,
                document_id=preserved.sha256,
                filename=filename,
                content_type=content_type.partition(";")[0].lower(),
                size_bytes=preserved.size_bytes,
                source_path=preserved.path,
                configuration_fingerprint=self.settings.fingerprint,
                configuration=self.settings.fingerprint_payload(),
            )
            registration_started = True
            view = await self._repository.register_submission(submission)
            await self._worker.enqueue_reserved(WorkItem(run_id, preserved.path), anonymous=True)
            return view
        except BaseException:
            await cleanup_submission(
                run_id=run_id,
                registration_started=registration_started,
                artifact_root=self.settings.artifact_root,
                repository=self._repository,
                offloads=self._offloads,
                uploads=self._uploads,
            )
            await self._worker.release_reservation()
            raise

    @asynccontextmanager
    async def _admit_mutation(self) -> AsyncIterator[None]:
        async with self._admission_condition:
            self._require_ready()
            self._active_mutations += 1
        try:
            yield
        finally:
            async with self._admission_condition:
                self._active_mutations -= 1
                if self._active_mutations == 0:
                    self._admission_condition.notify_all()

    async def _shutdown(self) -> None:
        if self._startup_task is not None:
            await asyncio.shield(self._startup_task)
        async with self._admission_condition:
            while self._active_mutations:
                await self._admission_condition.wait()
        try:
            await self._worker.stop(self.settings.shutdown_grace_seconds)
        finally:
            try:
                await self._repository.drain_offloads()
            finally:
                await self._offloads.drain()
                self._artifact_lock.release()
                self._readiness_reason = "stopped"
                self._closed = True

    async def _wait_for_terminal(self, run_id: str, timeout: float | None) -> RunView:
        return await wait_for_terminal(
            repository=self._repository,
            worker=self._worker,
            shutdown_started=self._shutdown_started,
            require_ready=self._require_ready,
            run_id=run_id,
            timeout=timeout,
        )
