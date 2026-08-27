"""Failed results report progress from the authoritative durable run."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from document_processing.processing import (
    DocumentProcessor,
    FailureCode,
    ProcessingFailure,
    RecoveryCheckpoint,
    RunHandle,
)
from document_processing.runtime.processing_repository import DurableProcessingRepository
from tests.processing.fakes import (
    CONFIG_FINGERPRINT,
    DOCUMENT_ID,
    FakePdfIntake,
    FakeRepository,
    QueueAnalyzer,
    prepared_pdf,
)


class ProgressRepository(FakeRepository):
    """Keep fake-owned durable progress when the run becomes failed."""

    def fail_run(self, run_id: str, failure: object) -> None:
        completed = self.handle.completed_pages
        total = self.handle.total_pages
        super().fail_run(run_id, failure)
        self.handle = replace(
            self.handle,
            completed_pages=completed,
            total_pages=total,
        )


class FailingInitializationRepository(ProgressRepository):
    def initialize_run(self, run_id: str, *, page_count: int) -> RecoveryCheckpoint:
        self.handle = replace(
            self.handle,
            completed_pages=0,
            total_pages=page_count,
        )
        raise OSError("injected initial-checkpoint failure")


class FailingRecoveryRepository(ProgressRepository):
    def recover_run(self, run_id: str, *, prepared: Any) -> RecoveryCheckpoint:
        raise OSError("injected recovery audit failure")


def test_post_render_failure_reports_durable_total_pages(tmp_path: Path) -> None:
    prepared = prepared_pdf(tmp_path, 3)
    repository = FailingInitializationRepository(tmp_path, prepared)
    intake = FakePdfIntake(prepared)
    processor = DocumentProcessor(
        analyzer=QueueAnalyzer([]),
        pdf_intake=intake,
        repository=repository,
    )

    result = asyncio.run(processor.process_pdf(tmp_path / "input.pdf"))

    assert result.status == "failed"
    assert result.completed_pages == 0
    assert result.total_pages == 3
    assert result.failure is not None
    assert result.failure.code is FailureCode.STORAGE_FAILED
    assert intake.prepare_preserved_calls


def test_resume_failure_after_render_reports_durable_prefix(tmp_path: Path) -> None:
    prepared = prepared_pdf(tmp_path, 2)
    repository = FailingRecoveryRepository(tmp_path, prepared)
    repository.handle = RunHandle(
        "run-1",
        tmp_path / "runs/run-1",
        DOCUMENT_ID,
        rendered=True,
        initialized=True,
        status="failed",
        failure=ProcessingFailure(FailureCode.MODEL_PERMANENT, "prior failure"),
        completed_pages=1,
        total_pages=2,
    )
    intake = FakePdfIntake(prepared)
    processor = DocumentProcessor(
        analyzer=QueueAnalyzer([]),
        pdf_intake=intake,
        repository=repository,
    )

    result = asyncio.run(processor.resume("run-1"))

    assert result.status == "failed"
    assert result.completed_pages == 1
    assert result.total_pages == 2
    assert result.failure is not None
    assert result.failure.code is FailureCode.INTEGRITY_FAILED
    assert intake.verify_calls == [(tmp_path / "runs/run-1", DOCUMENT_ID)]


@dataclass(frozen=True)
class StubPaths:
    root: Path


class StubStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def get_run(self, run_id: str) -> dict[str, object]:
        return {
            "run_id": run_id,
            "source_sha256": DOCUMENT_ID,
            "render_manifest_sha256": "c" * 64,
            "head_commit_id": "commit-1",
            "status": "failed",
            "failure_code": FailureCode.MODEL_PERMANENT.value,
            "failure_detail": "provider rejected the request",
            "failure_class": "permanent_model",
            "config_sha256": CONFIG_FINGERPRINT,
            "head_page": 2,
            "total_pages": 5,
        }

    def paths_for_run(self, run_id: str) -> StubPaths:
        return StubPaths(self.root / "runs" / run_id)


def test_durable_adapter_exposes_journal_progress(tmp_path: Path) -> None:
    storage = StubStorage(tmp_path)
    repository = DurableProcessingRepository(
        cast(Any, storage),
        {},
        CONFIG_FINGERPRINT,
    )

    handle = repository.load_run("run-1")

    assert handle.completed_pages == 2
    assert handle.total_pages == 5
