"""Cancellation ordering around the atomic failed-to-resuming transition."""

from __future__ import annotations

import asyncio
from pathlib import Path

from document_processing.processing import (
    CancellationToken,
    DocumentProcessor,
    FailureCode,
    RunHandle,
)
from tests.processing.factories import analysis_result
from tests.processing.fakes import (
    DOCUMENT_ID,
    FakePdfIntake,
    FakeRepository,
    QueueAnalyzer,
    prepared_pdf,
)


def _processor(
    root: Path,
    analyzer: QueueAnalyzer,
) -> tuple[DocumentProcessor, FakeRepository, FakePdfIntake]:
    prepared = prepared_pdf(root, 1)
    repository = FakeRepository(root, prepared)
    repository.handle = RunHandle(
        "run-1",
        root / "runs/run-1",
        DOCUMENT_ID,
        rendered=True,
        initialized=True,
        status="failed",
    )
    intake = FakePdfIntake(prepared)
    return (
        DocumentProcessor(
            analyzer=analyzer,
            pdf_intake=intake,
            repository=repository,
        ),
        repository,
        intake,
    )


def test_resume_atomically_clears_prior_durable_cancellation(tmp_path: Path) -> None:
    analyzer = QueueAnalyzer([analysis_result(1)])
    processor, repository, _ = _processor(tmp_path, analyzer)
    repository.durable_cancel = True

    result = asyncio.run(processor.resume("run-1"))

    assert result.status == "completed"
    assert repository.run_transitions == [True, False]
    assert [call.page_number for call in analyzer.calls] == [1]


def test_caller_cancellation_prevents_atomic_resume_transition(tmp_path: Path) -> None:
    processor, repository, intake = _processor(tmp_path, QueueAnalyzer([]))
    token = CancellationToken()
    token.cancel("shutdown requested")

    result = asyncio.run(processor.resume("run-1", token))

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.CANCELLED
    assert repository.run_transitions == []
    assert intake.verify_calls == []


def test_cancellation_after_atomic_resume_transition_wins(tmp_path: Path) -> None:
    analyzer = QueueAnalyzer([])
    processor, repository, intake = _processor(tmp_path, analyzer)
    repository.durable_cancel = True
    repository.after_run_transition = lambda resuming: setattr(
        repository,
        "durable_cancel",
        resuming,
    )

    result = asyncio.run(processor.resume("run-1"))

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.CANCELLED
    assert repository.run_transitions == [True]
    assert intake.verify_calls == []
    assert analyzer.calls == []


def test_service_preclaimed_resume_does_not_claim_twice(tmp_path: Path) -> None:
    analyzer = QueueAnalyzer([analysis_result(1)])
    processor, repository, _ = _processor(tmp_path, analyzer)
    repository.handle = RunHandle(
        "run-1",
        tmp_path / "runs/run-1",
        DOCUMENT_ID,
        rendered=True,
        initialized=True,
        status="resuming",
    )

    result = asyncio.run(
        processor.process_run(
            run_id="run-1",
            source_path=repository.handle.run_directory / "source/original.pdf",
            cancellation=CancellationToken(),
            resume=True,
        )
    )

    assert result.status == "completed"
    assert repository.run_transitions == [False]


def test_cancelled_preclaimed_resume_is_not_resurrected(tmp_path: Path) -> None:
    analyzer = QueueAnalyzer([])
    processor, repository, intake = _processor(tmp_path, analyzer)
    repository.durable_cancel = True

    result = asyncio.run(
        processor.process_run(
            run_id="run-1",
            source_path=repository.handle.run_directory / "source/original.pdf",
            cancellation=CancellationToken(),
            resume=True,
        )
    )

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.CANCELLED
    assert repository.run_transitions == []
    assert intake.verify_calls == []
    assert analyzer.calls == []
