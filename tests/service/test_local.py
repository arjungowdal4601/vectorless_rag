"""Local run service lifecycle and worker tests."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest

from document_processing.api.contracts import LocalRunServiceProtocol
from document_processing.config import Settings
from document_processing.contracts import RunStatus
from document_processing.errors import (
    InvalidPdfError,
    QueueCapacityError,
    UploadTooLargeError,
)
from document_processing.service.local import LocalRunService
from document_processing.service.models import FailureView, RecoveryItem
from tests.service.fakes import AcceptingValidator, FakeProcessor, FakeRepository, run_view


async def upload(payload: bytes = b"%PDF-fake") -> AsyncIterator[bytes]:
    yield payload


def build_service(
    tmp_path: Path,
    *,
    blocked: bool = False,
    capacity: int = 10,
    shutdown_grace_seconds: float = 0.01,
    max_upload_bytes: int = 100 * 1024 * 1024,
    validator: object | None = None,
) -> tuple[LocalRunService, FakeRepository, FakeProcessor]:
    repository = FakeRepository()
    processor = FakeProcessor(repository, blocked=blocked)
    settings = Settings(
        artifact_root=tmp_path / "artifacts",
        worker_queue_capacity=capacity,
        shutdown_grace_seconds=shutdown_grace_seconds,
        max_upload_bytes=max_upload_bytes,
    )
    service = LocalRunService(
        settings=settings,
        repository=repository,
        processor=processor,
        submission_validator=validator or AcceptingValidator(),  # type: ignore[arg-type]
    )
    return service, repository, processor


def test_submit_preserves_exact_source_and_waits_for_completion(tmp_path: Path) -> None:
    async def scenario() -> None:
        service, repository, processor = build_service(tmp_path)
        await service.start()
        assert isinstance(service, LocalRunServiceProtocol)

        payload = b"%PDF-1.7\nbyte-for-byte\x00"
        accepted = await service.submit_pdf(
            filename="../report.pdf", content_type="application/pdf", chunks=upload(payload)
        )
        finished = await service.wait(accepted.run_id, timeout=1)

        assert finished.status is RunStatus.COMPLETED
        assert accepted.document_id == hashlib.sha256(payload).hexdigest()
        submission = repository.submissions[0]
        assert submission.filename == "report.pdf"
        assert submission.source_path.read_bytes() == payload
        assert processor.calls == [(accepted.run_id, False)]
        assert await service.get_manifest(accepted.run_id) == {
            "run_id": accepted.run_id,
            "status": "completed",
        }
        await service.close()

    asyncio.run(scenario())


def test_worker_is_fifo_and_queue_capacity_is_bounded(tmp_path: Path) -> None:
    async def scenario() -> None:
        service, _repository, processor = build_service(tmp_path, blocked=True, capacity=2)
        await service.start()
        first = await service.submit_pdf(
            filename="1.pdf", content_type="application/pdf", chunks=upload()
        )
        assert await asyncio.wait_for(processor.started.get(), 1) == first.run_id
        second = await service.submit_pdf(
            filename="2.pdf", content_type="application/pdf", chunks=upload()
        )
        third = await service.submit_pdf(
            filename="3.pdf", content_type="application/pdf", chunks=upload()
        )
        with pytest.raises(QueueCapacityError):
            await service.submit_pdf(
                filename="4.pdf", content_type="application/pdf", chunks=upload()
            )

        processor.release.set()
        await service.wait(first.run_id, timeout=1)
        await service.wait(second.run_id, timeout=1)
        await service.wait(third.run_id, timeout=1)
        assert [run_id for run_id, _resume in processor.calls] == [
            first.run_id,
            second.run_id,
            third.run_id,
        ]
        await service.close()

    asyncio.run(scenario())


def test_active_cancellation_finishes_as_failed_cancelled(tmp_path: Path) -> None:
    async def scenario() -> None:
        service, _repository, processor = build_service(tmp_path, blocked=True)
        await service.start()
        accepted = await service.submit_pdf(
            filename="cancel.pdf", content_type="application/pdf", chunks=upload()
        )
        await asyncio.wait_for(processor.started.get(), 1)

        requested = await service.cancel_run(accepted.run_id)
        assert requested.cancel_requested
        finished = await service.wait(accepted.run_id, timeout=1)

        assert finished.status is RunStatus.FAILED
        assert finished.failure is not None
        assert finished.failure.code == "cancelled"
        await service.close()

    asyncio.run(scenario())


def test_queued_cancellation_is_visible_before_safe_resume(tmp_path: Path) -> None:
    async def scenario() -> None:
        service, _repository, processor = build_service(
            tmp_path,
            blocked=True,
            capacity=2,
        )
        await service.start()
        first = await service.submit_pdf(
            filename="first.pdf", content_type="application/pdf", chunks=upload()
        )
        await asyncio.wait_for(processor.started.get(), 1)
        second = await service.submit_pdf(
            filename="second.pdf", content_type="application/pdf", chunks=upload()
        )

        cancelled = await service.cancel_run(second.run_id)
        assert cancelled.status is RunStatus.FAILED
        assert (await service.wait(second.run_id, timeout=0.1)).status is RunStatus.FAILED

        resume = asyncio.create_task(service.resume_run(second.run_id))
        await asyncio.sleep(0)
        assert not resume.done()
        processor.release.set()
        await service.wait(first.run_id, timeout=1)
        assert (await asyncio.wait_for(resume, timeout=1)).status is RunStatus.RESUMING
        assert (await service.wait(second.run_id, timeout=1)).status is RunStatus.COMPLETED
        assert processor.calls == [(first.run_id, False), (second.run_id, True)]
        await service.close()

    asyncio.run(scenario())


def test_stale_terminal_signal_cannot_wake_resumed_generation(tmp_path: Path) -> None:
    async def scenario() -> None:
        service, _repository, processor = build_service(tmp_path, blocked=True)
        await service.start()
        accepted = await service.submit_pdf(
            filename="generation.pdf", content_type="application/pdf", chunks=upload()
        )
        await asyncio.wait_for(processor.started.get(), 1)
        stale_generation, _event = service._worker.terminal_event(accepted.run_id)  # noqa: SLF001
        await service.cancel_run(accepted.run_id)
        assert (await service.wait(accepted.run_id, timeout=1)).status is RunStatus.FAILED

        await service.resume_run(accepted.run_id)
        await asyncio.wait_for(processor.started.get(), 1)
        waiting = asyncio.create_task(service.wait(accepted.run_id, timeout=1))
        await asyncio.sleep(0)
        service._worker.signal_terminal(  # noqa: SLF001
            accepted.run_id,
            stale_generation,
        )
        await asyncio.sleep(0)
        assert not waiting.done()

        processor.release.set()
        assert (await waiting).status is RunStatus.COMPLETED
        await service.close()

    asyncio.run(scenario())


def test_identical_uploads_create_distinct_runs_with_same_document_id(tmp_path: Path) -> None:
    async def scenario() -> None:
        service, _repository, _processor = build_service(tmp_path)
        await service.start()
        first = await service.submit_pdf(
            filename="first.pdf", content_type="application/pdf", chunks=upload()
        )
        await service.wait(first.run_id, timeout=1)
        second = await service.submit_pdf(
            filename="second.pdf", content_type="application/pdf", chunks=upload()
        )
        await service.wait(second.run_id, timeout=1)

        assert first.run_id != second.run_id
        assert first.document_id == second.document_id
        await service.close()

    asyncio.run(scenario())


def test_resume_audits_then_queues_failed_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        service, repository, processor = build_service(tmp_path)
        run_id = str(uuid4())
        source = service.settings.artifact_root / "runs" / run_id / "source" / "original.pdf"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"%PDF-existing")
        failure = FailureView("failed", "internal", "Failed.", False)
        repository.runs[run_id] = run_view(
            run_id, "a" * 64, RunStatus.FAILED, source=source, failure=failure
        )
        repository.sources[run_id] = source
        await service.start()

        resumed = await service.resume_run(run_id)
        finished = await service.wait(run_id, timeout=1)

        assert resumed.status is RunStatus.RESUMING
        assert finished.status is RunStatus.COMPLETED
        assert processor.calls == [(run_id, True)]
        await service.close()

    asyncio.run(scenario())


def test_startup_recovery_requeues_durable_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        service, repository, processor = build_service(tmp_path)
        run_id = str(uuid4())
        source = service.settings.artifact_root / "runs" / run_id / "source" / "original.pdf"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"%PDF-existing")
        repository.runs[run_id] = run_view(run_id, "b" * 64, RunStatus.RESUMING)
        repository.sources[run_id] = source
        repository.recoveries.append(RecoveryItem(run_id, source, resume=True))

        await service.start()
        finished = await service.wait(run_id, timeout=1)

        assert finished.status is RunStatus.COMPLETED
        assert processor.calls == [(run_id, True)]
        await service.close()

    asyncio.run(scenario())


def test_invalid_pdf_is_quarantined_before_registration(tmp_path: Path) -> None:
    class RejectingValidator:
        def validate(self, _path: Path) -> int:
            raise InvalidPdfError("Malformed PDF.")

    async def scenario() -> None:
        service, repository, _processor = build_service(tmp_path, validator=RejectingValidator())
        await service.start()
        with pytest.raises(InvalidPdfError):
            await service.submit_pdf(
                filename="bad.pdf", content_type="application/pdf", chunks=upload()
            )

        assert not repository.submissions
        quarantined = list((service.settings.artifact_root / "quarantine").iterdir())
        assert len(quarantined) == 1
        assert (quarantined[0] / "source" / "original.pdf").is_file()
        await service.close()

    asyncio.run(scenario())


def test_preservation_failure_is_quarantined_before_registration(tmp_path: Path) -> None:
    async def scenario() -> None:
        service, repository, _processor = build_service(tmp_path)
        await service.start()
        with pytest.raises(InvalidPdfError):
            await service.submit_pdf(
                filename="bad.pdf",
                content_type="application/pdf",
                chunks=upload(b"not-a-pdf"),
            )

        assert not repository.submissions
        assert not list((service.settings.artifact_root / "runs").iterdir())
        quarantined = list((service.settings.artifact_root / "quarantine").iterdir())
        assert len(quarantined) == 1
        await service.close()

    asyncio.run(scenario())


def test_oversize_preservation_failure_is_quarantined(tmp_path: Path) -> None:
    async def scenario() -> None:
        service, repository, _processor = build_service(tmp_path, max_upload_bytes=5)
        await service.start()
        with pytest.raises(UploadTooLargeError):
            await service.submit_pdf(
                filename="large.pdf",
                content_type="application/pdf",
                chunks=upload(b"%PDF-x"),
            )

        assert not repository.submissions
        assert not list((service.settings.artifact_root / "runs").iterdir())
        assert len(list((service.settings.artifact_root / "quarantine").iterdir())) == 1
        await service.close()

    asyncio.run(scenario())
