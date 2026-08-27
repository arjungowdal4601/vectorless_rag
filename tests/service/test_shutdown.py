"""Shutdown, storage-lock, and admission-barrier tests."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from document_processing.config import Settings
from document_processing.contracts import RunStatus
from document_processing.errors import ArtifactRootLockedError, ServiceNotReadyError
from document_processing.processing.offload import BlockingCallTracker
from document_processing.service.local import LocalRunService
from document_processing.service.lock import ArtifactRootLock
from document_processing.service.models import FailureView, RecoveryItem, RunView, Submission
from tests.service.fakes import AcceptingValidator, FakeProcessor, FakeRepository, run_view
from tests.service.test_local import build_service, upload


class GatedValidator:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def validate(self, _path: Path) -> int:
        self.started.set()
        assert self.release.wait(timeout=5)
        return 1


class GatedRecoveryRepository(FakeRepository):
    def __init__(self) -> None:
        super().__init__()
        self.recovery_started = asyncio.Event()
        self.recovery_release = asyncio.Event()

    async def recover_startup(self, configuration_fingerprint: str) -> list[RecoveryItem]:
        assert len(configuration_fingerprint) == 64
        self.recovery_started.set()
        await self.recovery_release.wait()
        return []


class GatedThreadRepository(FakeRepository):
    def __init__(self, *, gate_register: bool = False, gate_resume: bool = False) -> None:
        super().__init__()
        self.gate_register = gate_register
        self.gate_resume = gate_resume
        self.started = threading.Event()
        self.release = threading.Event()
        self.offloads = BlockingCallTracker()

    async def register_submission(self, submission: Submission) -> RunView:
        if not self.gate_register:
            return await super().register_submission(submission)
        return await self.offloads.run(self._register, submission)

    def _register(self, submission: Submission) -> RunView:
        self.started.set()
        assert self.release.wait(timeout=5)
        view = run_view(submission.run_id, submission.document_id)
        self.runs[submission.run_id] = view
        self.sources[submission.run_id] = submission.source_path
        self.submissions.append(submission)
        return view

    async def prepare_resume(self, run_id: str) -> tuple[RunView, Path]:
        if not self.gate_resume:
            return await super().prepare_resume(run_id)
        return await self.offloads.run(self._prepare_resume, run_id)

    def _prepare_resume(self, run_id: str) -> tuple[RunView, Path]:
        self.started.set()
        assert self.release.wait(timeout=5)
        updated = replace(
            self.runs[run_id],
            status=RunStatus.RESUMING,
            phase="resuming",
            resumable=False,
            cancel_requested=False,
            failure=None,
        )
        self.runs[run_id] = updated
        return updated, self.sources[run_id]

    async def drain_offloads(self) -> None:
        await self.offloads.drain()


async def wait_for_thread(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 2)


def service_with_repository(
    tmp_path: Path,
    repository: FakeRepository,
) -> LocalRunService:
    settings = Settings(
        artifact_root=tmp_path / "artifacts",
        shutdown_grace_seconds=0,
    )
    return LocalRunService(
        settings=settings,
        repository=repository,
        processor=FakeProcessor(repository),
        submission_validator=AcceptingValidator(),
    )


def test_shutdown_releases_lock_without_user_cancellation(tmp_path: Path) -> None:
    async def scenario() -> None:
        service, repository, processor = build_service(tmp_path, blocked=True)
        await service.start()
        accepted = await service.submit_pdf(
            filename="active.pdf", content_type="application/pdf", chunks=upload()
        )
        await asyncio.wait_for(processor.started.get(), 1)

        await service.close()

        interrupted = repository.runs[accepted.run_id]
        assert interrupted.status is RunStatus.RUNNING
        assert interrupted.failure is None
        replacement = ArtifactRootLock(service.settings.artifact_root)
        replacement.acquire()
        replacement.release()

    asyncio.run(scenario())


def test_shutdown_returns_when_active_run_finishes_within_grace(tmp_path: Path) -> None:
    async def scenario() -> None:
        service, _repository, processor = build_service(
            tmp_path,
            blocked=True,
            shutdown_grace_seconds=1,
        )
        await service.start()
        await service.submit_pdf(
            filename="finishing.pdf", content_type="application/pdf", chunks=upload()
        )
        await asyncio.wait_for(processor.started.get(), 1)

        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        processor.release.set()
        await asyncio.wait_for(closing, timeout=0.2)

    asyncio.run(scenario())


def test_second_process_lock_makes_service_not_ready(tmp_path: Path) -> None:
    async def scenario() -> None:
        service, _repository, _processor = build_service(tmp_path)
        owner = ArtifactRootLock(service.settings.artifact_root)
        owner.acquire()
        try:
            await service.start()
            ready = await service.readiness()
            assert not ready.ready
            assert ready.reason == "storage_unavailable"
        finally:
            await service.close()
            owner.release()

    asyncio.run(scenario())


def test_close_waits_for_admitted_upload_and_rejects_new_mutations(tmp_path: Path) -> None:
    validator = GatedValidator()

    async def scenario() -> None:
        service, repository, _processor = build_service(tmp_path, validator=validator)
        await service.start()
        submission = asyncio.create_task(
            service.submit_pdf(
                filename="gated.pdf",
                content_type="application/pdf",
                chunks=upload(),
            )
        )
        await wait_for_thread(validator.started)

        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        with pytest.raises(ServiceNotReadyError):
            await service.cancel_run(str(uuid4()))
        with pytest.raises(ArtifactRootLockedError):
            ArtifactRootLock(service.settings.artifact_root).acquire()
        assert not closing.done()

        validator.release.set()
        accepted = await submission
        await closing
        assert repository.submissions[0].run_id == accepted.run_id

    asyncio.run(scenario())


def test_cancelled_close_defers_cancellation_until_publishers_drain(tmp_path: Path) -> None:
    validator = GatedValidator()

    async def scenario() -> None:
        service, _repository, _processor = build_service(tmp_path, validator=validator)
        await service.start()
        submission = asyncio.create_task(
            service.submit_pdf(
                filename="gated.pdf",
                content_type="application/pdf",
                chunks=upload(),
            )
        )
        await wait_for_thread(validator.started)
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)

        assert not closing.done()
        with pytest.raises(ArtifactRootLockedError):
            ArtifactRootLock(service.settings.artifact_root).acquire()
        validator.release.set()
        await submission
        with pytest.raises(asyncio.CancelledError):
            await closing
        replacement = ArtifactRootLock(service.settings.artifact_root)
        replacement.acquire()
        replacement.release()

    asyncio.run(scenario())


def test_close_serializes_with_gated_startup_recovery(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = GatedRecoveryRepository()
        service = service_with_repository(tmp_path, repository)
        starting = asyncio.create_task(service.start())
        await repository.recovery_started.wait()

        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        with pytest.raises(ArtifactRootLockedError):
            ArtifactRootLock(service.settings.artifact_root).acquire()
        assert not closing.done()

        repository.recovery_release.set()
        await starting
        await closing
        assert not (await service.readiness()).ready
        replacement = ArtifactRootLock(service.settings.artifact_root)
        replacement.acquire()
        replacement.release()

    asyncio.run(scenario())


def test_cancelled_start_closes_after_gated_recovery_before_unlock(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = GatedRecoveryRepository()
        service = service_with_repository(tmp_path, repository)
        starting = asyncio.create_task(service.start())
        await repository.recovery_started.wait()
        starting.cancel()
        await asyncio.sleep(0)

        assert not starting.done()
        with pytest.raises(ArtifactRootLockedError):
            ArtifactRootLock(service.settings.artifact_root).acquire()
        repository.recovery_release.set()
        with pytest.raises(asyncio.CancelledError):
            await starting
        assert not (await service.readiness()).ready
        replacement = ArtifactRootLock(service.settings.artifact_root)
        replacement.acquire()
        replacement.release()

    asyncio.run(scenario())


def test_cancelled_register_thread_is_failed_before_lock_release(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = GatedThreadRepository(gate_register=True)
        service = service_with_repository(tmp_path, repository)
        await service.start()
        submission = asyncio.create_task(
            service.submit_pdf(
                filename="register.pdf",
                content_type="application/pdf",
                chunks=upload(),
            )
        )
        await wait_for_thread(repository.started)
        submission.cancel()
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        with pytest.raises(ArtifactRootLockedError):
            ArtifactRootLock(service.settings.artifact_root).acquire()

        repository.release.set()
        with pytest.raises(asyncio.CancelledError):
            await submission
        await closing
        registered = repository.submissions[0]
        assert repository.runs[registered.run_id].status is RunStatus.FAILED

    asyncio.run(scenario())


def test_cancelled_resume_thread_rolls_back_before_lock_release(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = GatedThreadRepository(gate_resume=True)
        service = service_with_repository(tmp_path, repository)
        run_id = str(uuid4())
        source = service.settings.artifact_root / "runs" / run_id / "source" / "original.pdf"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"%PDF-existing")
        repository.runs[run_id] = run_view(
            run_id,
            "a" * 64,
            RunStatus.FAILED,
            source=source,
            failure=FailureView("failed", "internal", "Failed.", False),
        )
        repository.sources[run_id] = source
        await service.start()
        resuming = asyncio.create_task(service.resume_run(run_id))
        await wait_for_thread(repository.started)
        resuming.cancel()
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        with pytest.raises(ArtifactRootLockedError):
            ArtifactRootLock(service.settings.artifact_root).acquire()

        repository.release.set()
        with pytest.raises(asyncio.CancelledError):
            await resuming
        await closing
        rolled_back = repository.runs[run_id]
        assert rolled_back.status is RunStatus.FAILED
        assert rolled_back.failure is not None
        assert rolled_back.failure.code == "resume_abandoned"

    asyncio.run(scenario())
