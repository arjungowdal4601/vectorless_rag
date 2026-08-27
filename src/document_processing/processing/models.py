"""Public result models for deterministic processing operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from document_processing.contracts import ProcessingManifest, RunStatus
from document_processing.processing.errors import ProcessingFailure

TerminalStatus = Literal["completed", "failed"]


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    """Terminal outcome from processing a new PDF or resuming a failed run."""

    run_id: str
    document_id: str | None
    status: TerminalStatus
    completed_pages: int
    total_pages: int
    artifact_root: Path
    manifest_path: Path | None = None
    manifest: ProcessingManifest | None = None
    failure: ProcessingFailure | None = None

    def __post_init__(self) -> None:
        if self.status not in ("completed", "failed"):
            raise ValueError("processing result status must be terminal")
        if self.document_id is not None and not _is_sha256(self.document_id):
            raise ValueError("document_id must be a full lowercase SHA-256")
        if self.completed_pages < 0 or self.total_pages < 0:
            raise ValueError("page counts must be non-negative")
        if self.completed_pages > self.total_pages:
            raise ValueError("completed_pages cannot exceed total_pages")
        if self.status == "completed":
            if self.total_pages < 1:
                raise ValueError("completed results require a non-empty PDF")
            if self.manifest is None or self.failure is not None:
                raise ValueError("completed results require only a manifest")
            if self.completed_pages != self.total_pages:
                raise ValueError("completed results require every page")
            if self.manifest_path is None:
                raise ValueError("completed results require a manifest path")
            if self.document_id is None:
                raise ValueError("completed results require a document identity")
            if self.manifest_path != self.artifact_root / "final/output_manifest.json":
                raise ValueError("completed manifest_path must use the stable final path")
            if self.manifest.document_id != self.document_id:
                raise ValueError("result and manifest document identities must match")
            if self.manifest.run_id != self.run_id:
                raise ValueError("result and manifest run identities must match")
            if self.manifest.status is not RunStatus.COMPLETED:
                raise ValueError("completed result requires a completed manifest")
            if self.manifest.page_count != self.total_pages:
                raise ValueError("result and manifest page counts must match")
        elif self.manifest is not None or self.manifest_path is not None or self.failure is None:
            raise ValueError("failed results require only a failure")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
