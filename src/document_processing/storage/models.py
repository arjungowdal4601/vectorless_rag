"""Public value objects and errors for durable document-processing storage."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

JsonValue = Any
FaultHook = Callable[[str], None]
CancelCheck = Callable[[], bool]

FINAL_FILES = (
    "output_manifest.json",
    "short_term_memory.json",
    "document_structure.json",
    "model_usage.json",
    "processing_status.json",
    "recovery_events.json",
)


class StorageError(RuntimeError):
    """Base class for repository failures."""


class RunNotFound(StorageError):
    """The requested run does not exist."""


class InvalidTransition(StorageError):
    """A state transition would violate the run state machine."""


class CancelledBeforeCommit(StorageError):
    """Cancellation was observed at the durable page commit boundary."""


class IntegrityError(StorageError):
    """Durable state or a referenced artifact failed validation."""


@dataclass(frozen=True)
class RunPaths:
    """Filesystem locations belonging to one run."""

    root: Path
    source_dir: Path
    page_images_dir: Path
    initial_dir: Path
    page_artifacts_dir: Path
    staging_dir: Path
    quarantine_dir: Path
    final_dir: Path

    @property
    def source_pdf(self) -> Path:
        return self.source_dir / "original.pdf"

    @property
    def render_manifest(self) -> Path:
        return self.root / "render_manifest.json"

    @property
    def run_manifest(self) -> Path:
        return self.root / "run.json"


@dataclass(frozen=True)
class RenderPage:
    page_number: int
    path: str
    sha256: str
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class PageCommitInput:
    """All artifacts that become visible in one page-head transaction."""

    run_id: str
    page_number: int
    attempt_id: str
    page: JsonValue
    short_term_memory: JsonValue
    document_structure: JsonValue
    model_response: JsonValue
    usage: JsonValue = field(default_factory=dict)
    cancel_check: CancelCheck | None = None
    fault_hook: FaultHook | None = None


@dataclass(frozen=True)
class CommitRecord:
    commit_id: str
    run_id: str
    page_number: int
    relative_path: str
    manifest_sha256: str
    page_json_path: str | None
    short_term_memory_path: str
    document_structure_path: str


@dataclass(frozen=True)
class AuditIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True)
class AuditReport:
    run_id: str
    ok: bool
    head_page: int
    first_incomplete_page: int | None
    issues: Sequence[AuditIssue] = field(default_factory=tuple)
    orphan_paths: Sequence[str] = field(default_factory=tuple)

    def require_ok(self) -> AuditReport:
        if not self.ok:
            detail = "; ".join(f"{item.code}: {item.message}" for item in self.issues)
            raise IntegrityError(detail or f"run {self.run_id} failed audit")
        return self


@dataclass(frozen=True)
class RecoveryReport:
    audit: AuditReport
    quarantined: Sequence[str] = field(default_factory=tuple)
    reset_page: int | None = None


@dataclass(frozen=True)
class FinalManifest:
    run_id: str
    relative_path: str
    sha256: str
    payload: Mapping[str, Any]
