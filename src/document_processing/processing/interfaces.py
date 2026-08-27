"""Structural ports consumed by the deterministic document processor."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from document_processing.processing.errors import ProcessingFailure

if TYPE_CHECKING:
    from document_processing.contracts import (
        DocumentStructure,
        ModelResponse,
        ModelUsageRecord,
        PageArtifact,
        ProcessingManifest,
        ShortTermMemory,
        ShortTermMemoryState,
    )


@runtime_checkable
class AnalysisResult(Protocol):
    """Provider-validated output returned by the stateless analyzer."""

    @property
    def model_response(self) -> ModelResponse: ...

    @property
    def usage(self) -> ModelUsageRecord: ...


@runtime_checkable
class Analyzer(Protocol):
    """Analyze exactly one page using only the explicitly supplied context."""

    async def analyze(
        self,
        *,
        page_number: int,
        page_image_path: Path,
        short_term_memory: Mapping[str, object],
        attempt_number: int = 1,
    ) -> AnalysisResult:
        """Return a strict structured response and usage for one page."""


class RenderedPage(Protocol):
    """The manifest subset needed to bind a page to its stable path."""

    @property
    def page_number(self) -> int: ...

    @property
    def image_path(self) -> str: ...


class RenderManifest(Protocol):
    """The verified ordered render manifest subset used by processing."""

    @property
    def page_count(self) -> int: ...

    @property
    def pages(self) -> Sequence[RenderedPage]: ...


class PreparedPdf(Protocol):
    """A preserved and completely rendered source document."""

    @property
    def document_id(self) -> str: ...

    @property
    def source_path(self) -> Path: ...

    @property
    def source_size_bytes(self) -> int: ...

    @property
    def manifest_path(self) -> Path: ...

    @property
    def manifest(self) -> RenderManifest: ...

    @property
    def page_image_paths(self) -> Sequence[Path]: ...


class StoredPdf(Protocol):
    """A source durably preserved at the canonical run path."""

    @property
    def document_id(self) -> str: ...

    @property
    def source_path(self) -> Path: ...

    @property
    def source_size_bytes(self) -> int: ...


@runtime_checkable
class PdfIntake(Protocol):
    """Preserve/render a new PDF or verify an existing rendered run."""

    def preserve_path(self, source_path: Path, run_dir: Path) -> StoredPdf:
        """Durably preserve a path without starting render work."""

    def prepare_preserved_path(
        self,
        run_dir: Path,
        expected_document_id: str | None = None,
    ) -> PreparedPdf:
        """Render the already-preserved canonical source."""

    def prepare_path(self, source_path: Path, run_dir: Path) -> PreparedPdf:
        """Compatibility operation that preserves and renders in one call."""

    def verify_prepared(
        self,
        run_dir: Path,
        expected_document_id: str | None = None,
    ) -> PreparedPdf:
        """Verify source and rendered artifacts without changing them."""


@dataclass(frozen=True, slots=True)
class RunHandle:
    """Repository-owned identity and filesystem boundary for one run."""

    run_id: str
    run_directory: Path
    document_id: str | None = None
    rendered: bool = False
    initialized: bool = False
    status: str | None = None
    failure: ProcessingFailure | None = None
    completed_pages: int | None = None
    total_pages: int | None = None


@dataclass(frozen=True, slots=True)
class RecoveryCheckpoint:
    """A verified, contiguous prefix and its paired state snapshot."""

    completed_through: int
    short_term_memory: ShortTermMemoryState
    document_structure: DocumentStructure
    next_attempt_number: int = 1

    @property
    def next_page_number(self) -> int:
        return self.completed_through + 1


@dataclass(frozen=True, slots=True)
class PageCommit:
    """All values that must become durable together for one page."""

    run_id: str
    attempt_id: str
    page_number: int
    page_artifact: PageArtifact
    short_term_memory: ShortTermMemory
    document_structure: DocumentStructure
    model_response: ModelResponse
    usage: ModelUsageRecord


@dataclass(frozen=True, slots=True)
class PageAttempt:
    """Repository-assigned durable identity and ordinal for one model request."""

    attempt_id: str
    attempt_number: int


@dataclass(frozen=True, slots=True)
class CompletionAudit:
    """Repository proof used to gate publication of the final manifest."""

    complete: bool
    manifest: ProcessingManifest | None
    issues: tuple[str, ...] = ()
    published: bool = False


CancelCheck = Callable[[], bool]


@runtime_checkable
class ProcessingRepository(Protocol):
    """High-level persistence facade; concrete SQLite/filesystem code adapts here."""

    def create_run(self) -> RunHandle:
        """Allocate a durable not-started run and its artifact directory."""

    def load_run(self, run_id: str) -> RunHandle:
        """Load a previously durable run."""

    def record_source(self, run_id: str, stored: StoredPdf) -> None:
        """Journal the canonical source identity before rendering starts."""

    def record_render_manifest(self, run_id: str, prepared: PreparedPdf) -> None:
        """Bind a complete verified render manifest to its journaled source."""

    def record_prepared_pdf(self, run_id: str, prepared: PreparedPdf) -> None:
        """Compatibility operation binding both source and render metadata."""

    def initialize_run(
        self,
        run_id: str,
        *,
        page_count: int,
    ) -> RecoveryCheckpoint:
        """Persist page rows and the two initial document artifacts."""

    def recover_run(
        self,
        run_id: str,
        *,
        prepared: PreparedPdf,
    ) -> RecoveryCheckpoint:
        """Audit and return only a valid contiguous completed prefix."""

    def mark_run_running(self, run_id: str, *, resuming: bool) -> None:
        """Enter running or resuming state through a legal transition."""

    def is_cancel_requested(self, run_id: str) -> bool:
        """Read the durable cancellation flag."""

    def begin_page_attempt(self, run_id: str, *, page_number: int) -> PageAttempt:
        """Mark the page running and return its durable identity and ordinal."""

    def fail_page_attempt(
        self,
        run_id: str,
        *,
        attempt_id: str,
        failure: object,
        will_retry: bool,
    ) -> None:
        """Record a failed model attempt; retrying attempts may re-enter running."""

    def discard_page_attempt(
        self,
        run_id: str,
        *,
        attempt_id: str,
        reason: str,
    ) -> None:
        """Discard an uncommitted response and restore its page to pending."""

    def commit_page(self, commit: PageCommit, *, cancel_check: CancelCheck) -> None:
        """Atomically commit page and paired state after an in-transaction check."""

    def fail_run(self, run_id: str, failure: object) -> None:
        """Durably mark a run failed with a sanitized failure."""

    def audit_completion(
        self,
        run_id: str,
        *,
        prepared: PreparedPdf,
    ) -> CompletionAudit:
        """Verify all required artifacts and construct the candidate manifest."""

    def finalize_completion(
        self,
        run_id: str,
        *,
        audit: CompletionAudit,
        cancel_check: CancelCheck,
    ) -> ProcessingManifest:
        """Durably publish the audited manifest and mark the run completed."""
