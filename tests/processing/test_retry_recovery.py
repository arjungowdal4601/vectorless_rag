"""Durable retry-budget tests spanning process restarts and resume."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from document_processing.processing import (
    DocumentProcessor,
    FailureCode,
    RetryPolicy,
    RunHandle,
)
from document_processing.runtime.processing_repository import (
    DurableProcessingRepository,
)
from tests.processing.factories import analysis_result, resumed_checkpoint
from tests.processing.fakes import (
    DOCUMENT_ID,
    FakeManifest,
    FakePdfIntake,
    FakePreparedPdf,
    FakeRenderPage,
    FakeRepository,
    HttpFailure,
    QueueAnalyzer,
    prepared_pdf,
)
from tests.storage.helpers import make_repository


def resumed_processor(
    root: Path,
    analyzer: QueueAnalyzer,
    *,
    next_attempt_number: int,
    page_count: int = 2,
) -> tuple[DocumentProcessor, FakeRepository]:
    prepared = prepared_pdf(root, page_count)
    repository = FakeRepository(root, prepared)
    repository.handle = RunHandle(
        "run-1",
        root / "runs/run-1",
        DOCUMENT_ID,
        rendered=True,
        initialized=True,
    )
    repository.checkpoint = replace(
        resumed_checkpoint(),
        next_attempt_number=next_attempt_number,
    )
    repository.attempt_counts[2] = next_attempt_number - 1
    processor = DocumentProcessor(
        analyzer=analyzer,
        pdf_intake=FakePdfIntake(prepared),
        repository=repository,
        retry_policy=RetryPolicy(base_delay_seconds=0),
    )
    return processor, repository


def test_resume_starts_with_repository_next_attempt_ordinal(tmp_path: Path) -> None:
    analyzer = QueueAnalyzer([analysis_result(2, attempt=2)])
    processor, repository = resumed_processor(
        tmp_path,
        analyzer,
        next_attempt_number=2,
    )

    result = asyncio.run(processor.resume("run-1"))

    assert result.status == "completed"
    assert [call.attempt_number for call in analyzer.calls] == [2]
    assert repository.commits[0].usage.attempt_number == 2
    assert repository.attempt_counts[2] == 2


def test_resume_consumes_only_remaining_transient_budget(tmp_path: Path) -> None:
    analyzer = QueueAnalyzer([HttpFailure(503), analysis_result(2, attempt=3)])
    processor, repository = resumed_processor(
        tmp_path,
        analyzer,
        next_attempt_number=2,
    )

    result = asyncio.run(processor.resume("run-1"))

    assert result.status == "completed"
    assert [call.attempt_number for call in analyzer.calls] == [2, 3]
    assert repository.attempt_counts[2] == 3
    assert repository.failed_attempts[0][2] is True


def test_resume_at_last_attempt_cannot_create_attempt_four(tmp_path: Path) -> None:
    analyzer = QueueAnalyzer([HttpFailure(503)])
    processor, repository = resumed_processor(
        tmp_path,
        analyzer,
        next_attempt_number=3,
    )

    result = asyncio.run(processor.resume("run-1"))

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.MODEL_TRANSIENT_EXHAUSTED
    assert [call.attempt_number for call in analyzer.calls] == [3]
    assert repository.attempt_counts[2] == 3


def test_exhausted_checkpoint_never_begins_another_attempt(tmp_path: Path) -> None:
    analyzer = QueueAnalyzer([])
    processor, repository = resumed_processor(
        tmp_path,
        analyzer,
        next_attempt_number=4,
    )

    result = asyncio.run(processor.resume("run-1"))

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.MODEL_TRANSIENT_EXHAUSTED
    assert analyzer.calls == []
    assert repository.attempt_counts[2] == 3


def test_repository_attempt_ordinal_mismatch_fails_closed(tmp_path: Path) -> None:
    analyzer = QueueAnalyzer([])
    processor, repository = resumed_processor(
        tmp_path,
        analyzer,
        next_attempt_number=2,
    )
    repository.attempt_counts[2] = 0

    result = asyncio.run(processor.resume("run-1"))

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.INTEGRITY_FAILED
    assert analyzer.calls == []
    assert repository.failed_attempts[0][0] == "attempt-2-1"


def test_analyzer_usage_must_match_durable_attempt(tmp_path: Path) -> None:
    analyzer = QueueAnalyzer([analysis_result(2, attempt=1)])
    processor, repository = resumed_processor(
        tmp_path,
        analyzer,
        next_attempt_number=2,
    )

    result = asyncio.run(processor.resume("run-1"))

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.INTEGRITY_FAILED
    assert repository.commits == []


def test_attempt_ordinal_resets_for_page_after_recovered_page(tmp_path: Path) -> None:
    analyzer = QueueAnalyzer([analysis_result(2, attempt=2), analysis_result(3, attempt=1)])
    processor, repository = resumed_processor(
        tmp_path,
        analyzer,
        next_attempt_number=2,
        page_count=3,
    )

    result = asyncio.run(processor.resume("run-1"))

    assert result.status == "completed"
    assert [call.attempt_number for call in analyzer.calls] == [2, 1]
    assert repository.attempt_counts == {2: 2, 3: 1}


def test_real_storage_adapter_recovers_next_durable_attempt(tmp_path: Path) -> None:
    config = {"model": "test:model", "schema_version": 1}
    storage = make_repository(tmp_path)
    paths = storage.paths_for_run("run-1")
    image = paths.page_images_dir / "page-0001.png"
    source_hash = str(storage.get_run("run-1")["source_sha256"])
    first_attempt = storage.begin_page_attempt("run-1", 1)
    storage.fail_page_attempt(
        "run-1",
        first_attempt,
        code="temporary_failure",
        detail="temporary model failure",
        failure_class="transient_model",
        will_retry=False,
    )
    fingerprint = str(storage.get_run("run-1")["config_sha256"])
    adapter = DurableProcessingRepository(storage, config, fingerprint)
    prepared = FakePreparedPdf(
        document_id=source_hash,
        source_path=paths.source_pdf,
        manifest_path=paths.render_manifest,
        manifest=FakeManifest(
            1,
            (FakeRenderPage(1, "page_images/page-0001.png"),),
        ),
        page_image_paths=(image,),
    )

    adapter.mark_run_running("run-1", resuming=True)
    checkpoint = adapter.recover_run("run-1", prepared=prepared)
    adapter.mark_run_running("run-1", resuming=False)
    recovered_attempt = adapter.begin_page_attempt("run-1", page_number=1)

    assert checkpoint.next_attempt_number == 2
    assert recovered_attempt.attempt_number == 2
