"""Uninterruptible source publication followed by journal binding."""

from __future__ import annotations

from pathlib import Path

from document_processing.processing.interfaces import (
    PdfIntake,
    ProcessingRepository,
    RunHandle,
    StoredPdf,
)


class SourceCheckpointError(Exception):
    """Source was preserved but its journal identity could not be committed."""

    def __init__(self, document_id: str) -> None:
        super().__init__("preserved source could not be journaled")
        self.document_id = document_id


def preserve_and_record_source(
    intake: PdfIntake,
    repository: ProcessingRepository,
    run: RunHandle,
    source_path: Path,
) -> StoredPdf:
    """Keep preservation and journal binding inside one tracked thread call."""

    stored = intake.preserve_path(source_path, run.run_directory)
    try:
        repository.record_source(run.run_id, stored)
    except Exception as error:
        raise SourceCheckpointError(stored.document_id) from error
    return stored
