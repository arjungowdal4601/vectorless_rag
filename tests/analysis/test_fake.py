from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from document_processing.analysis.contracts import AnalysisResult, Analyzer
from document_processing.analysis.fake import FakeAnalyzer
from document_processing.contracts import ModelResponse, ModelUsageRecord


def test_fake_records_isolated_call_and_returns_queued_result(tmp_path: Path) -> None:
    result = AnalysisResult(
        model_response=ModelResponse.model_construct(),
        usage=ModelUsageRecord.model_construct(),
    )
    analyzer = FakeAnalyzer([result])
    memory: dict[str, object] = {"Active Reading Position": {"current_subsection": "A"}}

    returned = asyncio.run(
        analyzer.analyze(
            page_number=3,
            page_image_path=tmp_path / "page.png",
            short_term_memory=memory,
            attempt_number=2,
        )
    )
    memory["later mutation"] = True

    assert isinstance(analyzer, Analyzer)
    assert returned is result
    assert analyzer.calls[0].page_number == 3
    assert analyzer.calls[0].attempt_number == 2
    assert "later mutation" not in analyzer.calls[0].short_term_memory


def test_fake_raises_queued_exception_and_exhaustion(tmp_path: Path) -> None:
    analyzer = FakeAnalyzer([RuntimeError("temporary")])

    with pytest.raises(RuntimeError, match="temporary"):
        asyncio.run(
            analyzer.analyze(
                page_number=1,
                page_image_path=tmp_path / "page.png",
                short_term_memory={},
            )
        )
    with pytest.raises(AssertionError, match="no queued outcome"):
        asyncio.run(
            analyzer.analyze(
                page_number=1,
                page_image_path=tmp_path / "page.png",
                short_term_memory={},
            )
        )
