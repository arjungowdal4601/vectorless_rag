"""Offline collaborators shared by processing orchestration tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from document_processing.analysis import AnalysisResult
from document_processing.contracts import (
    DocumentStructure,
    EmptyShortTermMemory,
    ProcessingManifest,
)
from document_processing.processing import (
    CompletionAudit,
    PageAttempt,
    PageCommit,
    ProcessingFailure,
    RecoveryCheckpoint,
    RunHandle,
)
from tests.processing.factories import completed_manifest

DOCUMENT_ID = "a" * 64
CONFIG_FINGERPRINT = "b" * 64


@dataclass(frozen=True, slots=True)
class FakeRenderPage:
    page_number: int
    image_path: str


@dataclass(frozen=True, slots=True)
class FakeManifest:
    page_count: int
    pages: Sequence[FakeRenderPage]


@dataclass(frozen=True, slots=True)
class FakePreparedPdf:
    document_id: str
    source_path: Path
    manifest_path: Path
    manifest: FakeManifest
    page_image_paths: Sequence[Path]
    source_size_bytes: int = 100


@dataclass(frozen=True, slots=True)
class FakeStoredPdf:
    document_id: str
    source_path: Path
    source_size_bytes: int = 100


def prepared_pdf(root: Path, page_count: int = 2) -> FakePreparedPdf:
    run_root = root / "runs/run-1"
    pages = tuple(
        FakeRenderPage(number, f"page_images/page-{number:04d}.png")
        for number in range(1, page_count + 1)
    )
    paths = tuple(run_root / page.image_path for page in pages)
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"page:{path.name}".encode())
    return FakePreparedPdf(
        document_id=DOCUMENT_ID,
        source_path=run_root / "source/original.pdf",
        manifest_path=run_root / "render_manifest.json",
        manifest=FakeManifest(page_count, pages),
        page_image_paths=paths,
    )


class FakePdfIntake:
    def __init__(self, prepared: FakePreparedPdf) -> None:
        self.prepared = prepared
        self.preserve_calls: list[tuple[Path, Path]] = []
        self.prepare_preserved_calls: list[tuple[Path, str | None]] = []
        self.prepare_calls: list[tuple[Path, Path]] = []
        self.verify_calls: list[tuple[Path, str | None]] = []

    def prepare_path(self, source_path: Path, run_dir: Path) -> FakePreparedPdf:
        self.prepare_calls.append((source_path, run_dir))
        return self.prepared

    def preserve_path(self, source_path: Path, run_dir: Path) -> FakeStoredPdf:
        self.preserve_calls.append((source_path, run_dir))
        return FakeStoredPdf(self.prepared.document_id, self.prepared.source_path)

    def prepare_preserved_path(
        self,
        run_dir: Path,
        expected_document_id: str | None = None,
    ) -> FakePreparedPdf:
        self.prepare_preserved_calls.append((run_dir, expected_document_id))
        return self.prepared

    def verify_prepared(
        self,
        run_dir: Path,
        expected_document_id: str | None = None,
    ) -> FakePreparedPdf:
        self.verify_calls.append((run_dir, expected_document_id))
        return self.prepared


@dataclass(frozen=True, slots=True)
class AnalyzerCall:
    page_number: int
    page_image_path: Path
    short_term_memory: Mapping[str, object]
    attempt_number: int


class QueueAnalyzer:
    def __init__(
        self,
        outcomes: Sequence[AnalysisResult | Exception],
        *,
        after_result: Callable[[], None] | None = None,
    ) -> None:
        self.outcomes = list(outcomes)
        self.after_result = after_result
        self.calls: list[AnalyzerCall] = []

    async def analyze(
        self,
        *,
        page_number: int,
        page_image_path: Path,
        short_term_memory: Mapping[str, object],
        attempt_number: int = 1,
    ) -> AnalysisResult:
        self.calls.append(
            AnalyzerCall(
                page_number,
                page_image_path,
                dict(short_term_memory),
                attempt_number,
            )
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if self.after_result is not None:
            self.after_result()
        return outcome


class FakeRepository:
    def __init__(self, root: Path, prepared: FakePreparedPdf) -> None:
        self.prepared = prepared
        self.handle = RunHandle("run-1", root / "runs/run-1")
        self.checkpoint = RecoveryCheckpoint(
            0,
            EmptyShortTermMemory(),
            DocumentStructure(pages=[]),
        )
        self.durable_cancel = False
        self.audit_complete = True
        self.commits: list[PageCommit] = []
        self.failed_attempts: list[tuple[str, ProcessingFailure, bool]] = []
        self.discarded_attempts: list[str] = []
        self.failures: list[ProcessingFailure] = []
        self.authoritative_failure: ProcessingFailure | None = None
        self.run_transitions: list[bool] = []
        self.after_run_transition: Callable[[bool], None] | None = None
        self.attempt_counts: dict[int, int] = {}
        self.commit_callback: Callable[[PageCommit], None] | None = None
        self.commit_error_before_durable: Exception | None = None
        self.commit_error_after_durable: Exception | None = None
        self.cancel_in_commit = False
        self.finalized = False
        self.finalize_error_after_publish: Exception | None = None

    def create_run(self) -> RunHandle:
        return self.handle

    def load_run(self, run_id: str) -> RunHandle:
        assert run_id == self.handle.run_id
        return self.handle

    def record_prepared_pdf(self, run_id: str, prepared: Any) -> None:
        self.record_source(run_id, prepared)
        self.record_render_manifest(run_id, prepared)

    def record_source(self, run_id: str, stored: Any) -> None:
        assert run_id == self.handle.run_id
        self.handle = RunHandle(
            run_id,
            self.handle.run_directory,
            stored.document_id,
            rendered=self.handle.rendered,
            initialized=self.handle.initialized,
            status=self.handle.status,
        )

    def record_render_manifest(self, run_id: str, prepared: Any) -> None:
        assert run_id == self.handle.run_id
        assert self.handle.document_id == prepared.document_id
        self.handle = RunHandle(
            run_id,
            self.handle.run_directory,
            prepared.document_id,
            rendered=True,
            initialized=self.handle.initialized,
            status=self.handle.status,
        )

    def initialize_run(self, run_id: str, *, page_count: int) -> RecoveryCheckpoint:
        assert run_id == self.handle.run_id
        assert page_count == self.prepared.manifest.page_count
        self.handle = RunHandle(
            run_id,
            self.handle.run_directory,
            self.handle.document_id,
            rendered=True,
            initialized=True,
            status=self.handle.status,
        )
        return self.checkpoint

    def recover_run(self, run_id: str, *, prepared: Any) -> RecoveryCheckpoint:
        assert run_id == self.handle.run_id
        assert prepared.document_id == self.prepared.document_id
        return self.checkpoint

    def mark_run_running(self, run_id: str, *, resuming: bool) -> None:
        assert run_id == self.handle.run_id
        if resuming:
            self.durable_cancel = False
        self.run_transitions.append(resuming)
        if self.after_run_transition is not None:
            self.after_run_transition(resuming)

    def is_cancel_requested(self, run_id: str) -> bool:
        assert run_id == self.handle.run_id
        return self.durable_cancel

    def begin_page_attempt(self, run_id: str, *, page_number: int) -> PageAttempt:
        assert run_id == self.handle.run_id
        count = self.attempt_counts.get(page_number, 0) + 1
        self.attempt_counts[page_number] = count
        return PageAttempt(f"attempt-{page_number}-{count}", count)

    def fail_page_attempt(
        self,
        run_id: str,
        *,
        attempt_id: str,
        failure: object,
        will_retry: bool,
    ) -> None:
        assert run_id == self.handle.run_id
        assert isinstance(failure, ProcessingFailure)
        self.failed_attempts.append((attempt_id, failure, will_retry))

    def discard_page_attempt(
        self,
        run_id: str,
        *,
        attempt_id: str,
        reason: str,
    ) -> None:
        assert run_id == self.handle.run_id
        assert reason == "cancelled"
        self.discarded_attempts.append(attempt_id)

    def commit_page(
        self,
        commit: PageCommit,
        *,
        cancel_check: Callable[[], bool],
    ) -> None:
        if self.commit_error_before_durable is not None:
            raise self.commit_error_before_durable
        if self.cancel_in_commit:
            self.durable_cancel = True
            raise CancelledBeforeCommit
        if cancel_check():
            raise CancelledBeforeCommit
        self.commits.append(commit)
        self.checkpoint = RecoveryCheckpoint(
            commit.page_number,
            commit.short_term_memory,
            commit.document_structure,
        )
        if self.commit_callback is not None:
            self.commit_callback(commit)
        if self.commit_error_after_durable is not None:
            raise self.commit_error_after_durable

    def fail_run(self, run_id: str, failure: object) -> None:
        assert run_id == self.handle.run_id
        assert isinstance(failure, ProcessingFailure)
        durable = self.authoritative_failure or failure
        self.failures.append(durable)
        self.handle = RunHandle(
            self.handle.run_id,
            self.handle.run_directory,
            self.handle.document_id,
            self.handle.rendered,
            self.handle.initialized,
            "failed",
            durable,
        )

    def audit_completion(self, run_id: str, *, prepared: Any) -> CompletionAudit:
        assert run_id == self.handle.run_id
        if not self.audit_complete:
            return CompletionAudit(False, None, ("missing page artifact",))
        return CompletionAudit(True, self._manifest(), published=self.finalized)

    def finalize_completion(
        self,
        run_id: str,
        *,
        audit: CompletionAudit,
        cancel_check: Callable[[], bool],
    ) -> ProcessingManifest:
        assert run_id == self.handle.run_id
        if cancel_check():
            raise CancelledBeforeCommit
        assert audit.manifest is not None
        self.finalized = True
        if self.finalize_error_after_publish is not None:
            raise self.finalize_error_after_publish
        return audit.manifest

    def _manifest(self) -> ProcessingManifest:
        return completed_manifest(
            document_id=DOCUMENT_ID,
            config_fingerprint=CONFIG_FINGERPRINT,
            run_id=self.handle.run_id,
            page_count=self.prepared.manifest.page_count,
            attempt_counts=self.attempt_counts,
        )


class CancelledBeforeCommit(Exception): ...


class HttpFailure(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
