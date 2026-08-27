"""End-to-end offline tests for deterministic page orchestration."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from document_processing.contracts import DocumentStructure, EmptyShortTermMemory
from document_processing.processing import (
    CancellationToken,
    DocumentProcessor,
    FailureCode,
    RecoveryCheckpoint,
    RetryPolicy,
)
from document_processing.processing.retry import Clock, Sleeper
from tests.processing.factories import analysis_result, model_response, resumed_checkpoint
from tests.processing.fakes import (
    DOCUMENT_ID,
    FakePdfIntake,
    FakeRepository,
    HttpFailure,
    QueueAnalyzer,
    prepared_pdf,
)


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


def build_processor(
    root: Path,
    analyzer: QueueAnalyzer,
    *,
    page_count: int = 2,
    retry_policy: RetryPolicy | None = None,
    clock: Clock | None = None,
    sleep: Sleeper | None = None,
) -> tuple[DocumentProcessor, FakeRepository, FakePdfIntake]:
    prepared = prepared_pdf(root, page_count)
    repository = FakeRepository(root, prepared)
    intake = FakePdfIntake(prepared)
    processor = DocumentProcessor(
        analyzer=analyzer,
        pdf_intake=intake,
        repository=repository,
        retry_policy=retry_policy,
        clock=clock or time.monotonic,
        random_source=lambda: 0.5,
        sleep=sleep or asyncio.sleep,
    )
    return processor, repository, intake


def test_processes_pages_sequentially_with_only_latest_memory(tmp_path: Path) -> None:
    analyzer = QueueAnalyzer([analysis_result(1), analysis_result(2)])
    processor, repository, _ = build_processor(tmp_path, analyzer)

    result = run(processor.process_pdf(tmp_path / "input.pdf"))

    assert result.status == "completed"
    assert result.completed_pages == 2
    assert result.artifact_root == tmp_path / "runs/run-1"
    assert result.manifest_path == (tmp_path / "runs/run-1/final/output_manifest.json")
    assert [call.page_number for call in analyzer.calls] == [1, 2]
    assert analyzer.calls[0].short_term_memory == {}
    second_memory = analyzer.calls[1].short_term_memory
    assert list(second_memory) == ["Active Reading Position"]
    active_position = second_memory["Active Reading Position"]
    assert isinstance(active_position, dict)
    assert active_position["current_subsection"] == "Section 1"
    assert [commit.page_number for commit in repository.commits] == [1, 2]
    assert repository.commits[0].page_artifact.topics[0].topic_id == "p0001-t001"
    assert repository.commits[1].page_artifact.assets[0].asset_id == "p0002-a001"
    final_structure = repository.commits[-1].document_structure
    assert [page.topics for page in final_structure.pages] == [
        ["Section 1"],
        ["Section 2"],
    ]
    assert repository.finalized


class FakeTime:
    def __init__(self, token: CancellationToken | None = None) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.token = token

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay
        if self.token is not None:
            self.token.cancel("test cancellation")


def test_retries_only_transient_failure_with_actual_attempt_number(
    tmp_path: Path,
) -> None:
    fake_time = FakeTime()
    analyzer = QueueAnalyzer([HttpFailure(429), analysis_result(1, attempt=2)])
    policy = RetryPolicy(base_delay_seconds=0.5, cancellation_poll_seconds=1.0)
    processor, repository, _ = build_processor(
        tmp_path,
        analyzer,
        page_count=1,
        retry_policy=policy,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )

    result = run(processor.process_pdf(tmp_path / "input.pdf"))

    assert result.status == "completed"
    assert [call.attempt_number for call in analyzer.calls] == [1, 2]
    assert fake_time.sleeps == [0.5]
    assert repository.failed_attempts[0][2] is True
    assert repository.commits[0].usage.attempt_number == 2


def test_stops_after_three_transient_attempts(tmp_path: Path) -> None:
    fake_time = FakeTime()
    analyzer = QueueAnalyzer([HttpFailure(503), TimeoutError(), ConnectionError()])
    policy = RetryPolicy(base_delay_seconds=0, cancellation_poll_seconds=1)
    processor, repository, _ = build_processor(
        tmp_path,
        analyzer,
        page_count=1,
        retry_policy=policy,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )

    result = run(processor.process_pdf(tmp_path / "input.pdf"))

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.MODEL_TRANSIENT_EXHAUSTED
    assert [call.attempt_number for call in analyzer.calls] == [1, 2, 3]
    assert [entry[2] for entry in repository.failed_attempts] == [True, True, False]
    assert not repository.commits


def test_invalid_memory_transition_is_permanent_and_not_retried(
    tmp_path: Path,
) -> None:
    invalid = analysis_result(1)
    invalid = type(invalid)(model_response(1, valid_transition=False), invalid.usage)
    analyzer = QueueAnalyzer([invalid])
    processor, repository, _ = build_processor(tmp_path, analyzer, page_count=1)

    result = run(processor.process_pdf(tmp_path / "input.pdf"))

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.MODEL_CONTRACT_INVALID
    assert len(analyzer.calls) == 1
    assert repository.failed_attempts[0][2] is False


def test_cancellation_after_model_return_discards_response(tmp_path: Path) -> None:
    token = CancellationToken()

    def cancel_after_result() -> None:
        token.cancel()

    analyzer = QueueAnalyzer([analysis_result(1)], after_result=cancel_after_result)
    processor, repository, _ = build_processor(tmp_path, analyzer, page_count=1)

    result = run(processor.process_pdf(tmp_path / "input.pdf", token))

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.CANCELLED
    assert repository.discarded_attempts == ["attempt-1-1"]
    assert not repository.commits


def test_pre_cancelled_run_never_enters_pdf_intake(tmp_path: Path) -> None:
    token = CancellationToken()
    token.cancel()
    processor, repository, intake = build_processor(tmp_path, QueueAnalyzer([]), page_count=1)

    result = run(processor.process_pdf(tmp_path / "input.pdf", token))

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.CANCELLED
    assert intake.preserve_calls == []
    assert repository.attempt_counts == {}


def test_cancellation_during_backoff_prevents_next_attempt(tmp_path: Path) -> None:
    token = CancellationToken()
    fake_time = FakeTime(token)
    analyzer = QueueAnalyzer([HttpFailure(429)])
    processor, repository, _ = build_processor(
        tmp_path,
        analyzer,
        page_count=1,
        retry_policy=RetryPolicy(
            base_delay_seconds=1,
            cancellation_poll_seconds=0.1,
        ),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )

    result = run(processor.process_pdf(tmp_path / "input.pdf", token))

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.CANCELLED
    assert len(analyzer.calls) == 1
    assert repository.attempt_counts == {1: 1}


def test_commit_boundary_cancellation_is_not_discarded_twice(tmp_path: Path) -> None:
    analyzer = QueueAnalyzer([analysis_result(1)])
    processor, repository, _ = build_processor(tmp_path, analyzer, page_count=1)
    repository.cancel_in_commit = True

    result = run(processor.process_pdf(tmp_path / "input.pdf"))

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.CANCELLED
    assert repository.discarded_attempts == []
    assert not repository.commits


def test_cancellation_between_pages_preserves_completed_prefix(tmp_path: Path) -> None:
    token = CancellationToken()
    analyzer = QueueAnalyzer([analysis_result(1)])
    processor, repository, _ = build_processor(tmp_path, analyzer)

    def cancel_after_commit(_: object) -> None:
        token.cancel()

    repository.commit_callback = cancel_after_commit

    result = run(processor.process_pdf(tmp_path / "input.pdf", token))

    assert result.status == "failed"
    assert result.completed_pages == 1
    assert [commit.page_number for commit in repository.commits] == [1]
    assert repository.attempt_counts == {1: 1}


def test_resume_reuses_contiguous_prefix_and_only_analyzes_next_page(
    tmp_path: Path,
) -> None:
    analyzer = QueueAnalyzer([analysis_result(2)])
    processor, repository, intake = build_processor(tmp_path, analyzer)
    repository.handle = type(repository.handle)(
        "run-1",
        tmp_path / "runs/run-1",
        DOCUMENT_ID,
        rendered=True,
        initialized=True,
    )
    repository.checkpoint = resumed_checkpoint()

    result = run(processor.resume("run-1"))

    assert result.status == "completed"
    assert repository.run_transitions == [True, False]
    assert intake.verify_calls == [(tmp_path / "runs/run-1", DOCUMENT_ID)]
    assert [call.page_number for call in analyzer.calls] == [2]
    active_position = analyzer.calls[0].short_term_memory["Active Reading Position"]
    assert isinstance(active_position, dict)
    assert active_position["current_subsection"] == "Section 1"
    commit = repository.commits[0]
    assert commit.page_artifact.topics[0].topic_id == "p0002-t001"
    assert len(commit.document_structure.pages) == 2


def test_process_run_uses_already_registered_run(tmp_path: Path) -> None:
    analyzer = QueueAnalyzer([analysis_result(1)])
    processor, repository, intake = build_processor(tmp_path, analyzer, page_count=1)
    repository.handle = type(repository.handle)("run-1", tmp_path / "runs/run-1", DOCUMENT_ID)
    token = CancellationToken()

    result = run(
        processor.process_run(
            run_id="run-1",
            source_path=tmp_path / "runs/run-1/source/original.pdf",
            cancellation=token,
            resume=False,
        )
    )

    assert result.status == "completed"
    assert intake.preserve_calls == []
    assert intake.prepare_preserved_calls == [
        (
            tmp_path / "runs/run-1",
            DOCUMENT_ID,
        )
    ]
    assert repository.run_transitions == [False]


def test_final_audit_blocks_completion(tmp_path: Path) -> None:
    analyzer = QueueAnalyzer([analysis_result(1)])
    processor, repository, _ = build_processor(tmp_path, analyzer, page_count=1)
    repository.audit_complete = False

    result = run(processor.process_pdf(tmp_path / "input.pdf"))

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.INTEGRITY_FAILED
    assert result.completed_pages == 1
    assert result.manifest is None
    assert not repository.finalized


def test_contradictory_checkpoint_fails_closed_before_analysis(
    tmp_path: Path,
) -> None:
    analyzer = QueueAnalyzer([])
    processor, repository, _ = build_processor(tmp_path, analyzer, page_count=1)
    repository.checkpoint = RecoveryCheckpoint(
        1,
        EmptyShortTermMemory(),
        DocumentStructure(pages=[]),
    )

    result = run(processor.process_pdf(tmp_path / "input.pdf"))

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.INTEGRITY_FAILED
    assert analyzer.calls == []
