"""Cancellation takes priority immediately after a model response."""

from __future__ import annotations

import asyncio
from pathlib import Path

from document_processing.processing import CancellationToken, DocumentProcessor, FailureCode
from tests.processing.factories import analysis_result
from tests.processing.fakes import FakePdfIntake, FakeRepository, QueueAnalyzer, prepared_pdf


def test_cancellation_beats_mismatched_usage_after_model_return(tmp_path: Path) -> None:
    token = CancellationToken()
    prepared = prepared_pdf(tmp_path, 1)
    repository = FakeRepository(tmp_path, prepared)
    analyzer = QueueAnalyzer([analysis_result(1, attempt=2)], after_result=token.cancel)
    processor = DocumentProcessor(
        analyzer=analyzer,
        pdf_intake=FakePdfIntake(prepared),
        repository=repository,
    )

    result = asyncio.run(processor.process_pdf(tmp_path / "input.pdf", token))

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.CANCELLED
    assert repository.discarded_attempts == ["attempt-1-1"]
    assert repository.failed_attempts == []
