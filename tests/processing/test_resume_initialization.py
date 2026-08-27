"""Recovery tests for failures that occurred before the initial checkpoint."""

from __future__ import annotations

import asyncio
from pathlib import Path

from document_processing.processing import CancellationToken, DocumentProcessor, RunHandle
from tests.processing.factories import analysis_result
from tests.processing.fakes import (
    DOCUMENT_ID,
    FakePdfIntake,
    FakeRepository,
    QueueAnalyzer,
    prepared_pdf,
)


def test_resume_rerenders_when_initial_checkpoint_was_never_committed(
    tmp_path: Path,
) -> None:
    prepared = prepared_pdf(tmp_path, 1)
    repository = FakeRepository(tmp_path, prepared)
    repository.handle = RunHandle(
        "run-1",
        tmp_path / "runs/run-1",
        DOCUMENT_ID,
        rendered=False,
        initialized=False,
    )
    intake = FakePdfIntake(prepared)
    analyzer = QueueAnalyzer([analysis_result(1)])
    processor = DocumentProcessor(
        analyzer=analyzer,
        pdf_intake=intake,
        repository=repository,
    )

    result = asyncio.run(processor.resume("run-1", CancellationToken()))

    assert result.status == "completed"
    assert intake.prepare_calls == []
    assert intake.prepare_preserved_calls == [
        (
            tmp_path / "runs/run-1",
            DOCUMENT_ID,
        )
    ]
    assert intake.verify_calls == []
    assert repository.run_transitions == [True, False]


def test_resume_verifies_rendered_run_before_initializing_state(tmp_path: Path) -> None:
    prepared = prepared_pdf(tmp_path, 1)
    repository = FakeRepository(tmp_path, prepared)
    repository.handle = RunHandle(
        "run-1",
        tmp_path / "runs/run-1",
        DOCUMENT_ID,
        rendered=True,
        initialized=False,
    )
    intake = FakePdfIntake(prepared)
    processor = DocumentProcessor(
        analyzer=QueueAnalyzer([analysis_result(1)]),
        pdf_intake=intake,
        repository=repository,
    )

    result = asyncio.run(processor.resume("run-1"))

    assert result.status == "completed"
    assert intake.prepare_calls == []
    assert intake.prepare_preserved_calls == []
    assert intake.verify_calls == [(tmp_path / "runs/run-1", DOCUMENT_ID)]
