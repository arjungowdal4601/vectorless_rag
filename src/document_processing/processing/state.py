"""Pure model-response materialization for one sequential page."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from document_processing.contracts import (
    DocumentStructure,
    EmptyShortTermMemory,
    ModelResponse,
    PageArtifact,
    ShortTermMemory,
    ShortTermMemoryState,
    append_document_structure_page,
    apply_short_term_memory_edit,
    build_page_artifact,
)
from document_processing.processing.errors import PageContractError
from document_processing.processing.interfaces import PreparedPdf


@dataclass(frozen=True, slots=True)
class MaterializedPage:
    """System-owned page artifact and the paired next state."""

    page_artifact: PageArtifact
    short_term_memory: ShortTermMemory
    document_structure: DocumentStructure


def materialize_page(
    response: ModelResponse,
    *,
    page_number: int,
    page_image_path: str,
    short_term_memory: ShortTermMemoryState,
    document_structure: DocumentStructure,
) -> MaterializedPage:
    """Apply one response without coercion, repair, or deduplication."""

    try:
        edit = response.memory_edits.short_term_memory_edits[0]
        next_memory = apply_short_term_memory_edit(
            short_term_memory,
            edit,
            page_number=page_number,
        )
        next_structure = append_document_structure_page(
            document_structure,
            page_number=page_number,
            topics=response.memory_edits.document_structure_edits.topics,
        )
        page_artifact = build_page_artifact(
            response.page_output,
            page_number=page_number,
            page_image_path=page_image_path,
        )
    except (IndexError, TypeError, ValueError) as error:
        raise PageContractError(
            "model response violated a deterministic page transition"
        ) from error
    return MaterializedPage(page_artifact, next_memory, next_structure)


def memory_context(memory: ShortTermMemoryState) -> dict[str, object]:
    """Serialize only the latest short-term memory for the analyzer message."""

    return memory.model_dump(mode="json", by_alias=True)


def validate_checkpoint(
    *,
    completed_through: int,
    total_pages: int,
    short_term_memory: ShortTermMemoryState,
    document_structure: DocumentStructure,
    next_attempt_number: int = 1,
) -> None:
    """Fail closed when a recovered head and its paired snapshots disagree."""

    if completed_through < 0 or completed_through > total_pages:
        raise ValueError("checkpoint head is outside the document")
    if next_attempt_number < 1:
        raise ValueError("next attempt number must be positive")
    if completed_through == total_pages and next_attempt_number != 1:
        raise ValueError("a complete page prefix cannot retain a pending attempt")
    if len(document_structure.pages) != completed_through:
        raise ValueError("structure does not match the completed prefix")
    if completed_through == 0 and not isinstance(short_term_memory, EmptyShortTermMemory):
        raise ValueError("an empty prefix requires empty memory")
    if completed_through > 0 and not isinstance(short_term_memory, ShortTermMemory):
        raise ValueError("a completed prefix requires initialized memory")


def validate_prepared(prepared: PreparedPdf, run_directory: Path) -> None:
    """Reject contradictory render manifests before analysis or recovery."""

    if prepared.manifest_path != run_directory / "render_manifest.json":
        raise ValueError("render manifest path is not canonical for the run")
    if prepared.source_path != run_directory / "source/original.pdf":
        raise ValueError("source path is not canonical for the run")
    page_count = prepared.manifest.page_count
    if page_count < 1:
        raise ValueError("prepared PDF must contain at least one page")
    if len(prepared.page_image_paths) != page_count:
        raise ValueError("prepared image count does not match render manifest")
    if len(prepared.manifest.pages) != page_count:
        raise ValueError("render manifest page count is contradictory")
    for expected, page in enumerate(prepared.manifest.pages, start=1):
        if page.page_number != expected:
            raise ValueError("render manifest pages are not contiguous")
        if page.image_path != f"page_images/page-{expected:04d}.png":
            raise ValueError("render manifest page path is unstable")
        image = prepared.page_image_paths[expected - 1]
        canonical = run_directory / page.image_path
        if image != canonical:
            raise ValueError("prepared page images do not match manifest order and paths")
        cursor = image
        while cursor != run_directory:
            if cursor.is_symlink():
                raise ValueError("prepared page image path must not traverse a symbolic link")
            cursor = cursor.parent
        if run_directory.is_symlink():
            raise ValueError("prepared page image path must not traverse a symbolic link")
        try:
            mode = image.stat(follow_symlinks=False).st_mode
        except OSError as error:
            raise ValueError("prepared page image is missing") from error
        if not stat.S_ISREG(mode):
            raise ValueError("prepared page image must be a regular file")
