"""Direct results reflect the terminal failure that won durable races."""

from __future__ import annotations

import asyncio
from pathlib import Path

from document_processing.processing import DocumentProcessor, FailureCode, ProcessingFailure
from tests.processing.fakes import (
    FakePdfIntake,
    FakePreparedPdf,
    FakeRepository,
    QueueAnalyzer,
    prepared_pdf,
)


class FailingRender(FakePdfIntake):
    def prepare_preserved_path(
        self,
        run_dir: Path,
        expected_document_id: str | None = None,
    ) -> FakePreparedPdf:
        raise RuntimeError("render failed after cancellation won")


def test_durable_cancel_overrides_concurrent_render_failure(tmp_path: Path) -> None:
    prepared = prepared_pdf(tmp_path, 1)
    repository = FakeRepository(tmp_path, prepared)
    repository.authoritative_failure = ProcessingFailure(
        FailureCode.CANCELLED,
        "Document processing was cancelled.",
    )
    processor = DocumentProcessor(
        analyzer=QueueAnalyzer([]),
        pdf_intake=FailingRender(prepared),
        repository=repository,
    )

    result = asyncio.run(processor.process_pdf(tmp_path / "input.pdf"))

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.CANCELLED
    assert repository.failures[0].code is FailureCode.CANCELLED
