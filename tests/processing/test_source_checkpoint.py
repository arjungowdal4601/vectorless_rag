"""Source identity is journaled before fallible rendering begins."""

from __future__ import annotations

import asyncio
from pathlib import Path

from document_processing.processing import DocumentProcessor, FailureCode
from tests.processing.factories import analysis_result
from tests.processing.fakes import (
    DOCUMENT_ID,
    FakePdfIntake,
    FakePreparedPdf,
    FakeRepository,
    QueueAnalyzer,
    prepared_pdf,
)


class FailingRenderIntake(FakePdfIntake):
    def prepare_preserved_path(
        self,
        run_dir: Path,
        expected_document_id: str | None = None,
    ) -> FakePreparedPdf:
        self.prepare_preserved_calls.append((run_dir, expected_document_id))
        raise RuntimeError("injected render failure after source publication")


def test_render_failure_retains_journaled_document_identity(tmp_path: Path) -> None:
    prepared = prepared_pdf(tmp_path, 1)
    repository = FakeRepository(tmp_path, prepared)
    intake = FailingRenderIntake(prepared)
    processor = DocumentProcessor(
        analyzer=QueueAnalyzer([]),
        pdf_intake=intake,
        repository=repository,
    )

    result = asyncio.run(processor.process_pdf(tmp_path / "input.pdf"))

    assert result.status == "failed"
    assert result.document_id == DOCUMENT_ID
    assert result.failure is not None
    assert result.failure.code is FailureCode.PDF_INTAKE_FAILED
    assert repository.handle.document_id == DOCUMENT_ID
    assert not repository.handle.rendered
    assert not repository.handle.initialized


def test_explicit_resume_renders_journaled_source_after_render_failure(
    tmp_path: Path,
) -> None:
    prepared = prepared_pdf(tmp_path, 1)
    repository = FakeRepository(tmp_path, prepared)
    first = DocumentProcessor(
        analyzer=QueueAnalyzer([]),
        pdf_intake=FailingRenderIntake(prepared),
        repository=repository,
    )
    failed = asyncio.run(first.process_pdf(tmp_path / "input.pdf"))
    assert failed.status == "failed"

    intake = FakePdfIntake(prepared)
    resumed = DocumentProcessor(
        analyzer=QueueAnalyzer([analysis_result(1)]),
        pdf_intake=intake,
        repository=repository,
    )
    result = asyncio.run(resumed.resume("run-1"))

    assert result.status == "completed"
    assert result.document_id == DOCUMENT_ID
    assert intake.prepare_preserved_calls == [(tmp_path / "runs/run-1", DOCUMENT_ID)]
    assert repository.run_transitions == [False, True, False]
