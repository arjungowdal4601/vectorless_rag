"""Pure reducers for memory and document-structure state."""

from __future__ import annotations

from collections.abc import Sequence

from .memory import (
    ACTIVE_READING_POSITION,
    AppendNewSectionMemoryEdit,
    EmptyShortTermMemory,
    ReplaceSectionMemoryEdit,
    ShortTermMemory,
    ShortTermMemoryEdit,
    ShortTermMemoryState,
)
from .structure import DocumentStructure, DocumentStructurePage


class ContractTransitionError(ValueError):
    """A valid edit shape is illegal at the current processing phase."""


def _validate_page_number(page_number: int) -> None:
    if isinstance(page_number, bool) or not isinstance(page_number, int):
        raise TypeError("page_number must be an integer")
    if page_number < 1:
        raise ValueError("page_number must be at least 1")


def apply_short_term_memory_edit(
    current: ShortTermMemoryState,
    edit: ShortTermMemoryEdit,
    *,
    page_number: int,
) -> ShortTermMemory:
    """Create memory on page one and replace it on every later page."""

    _validate_page_number(page_number)
    if page_number == 1:
        if not isinstance(current, EmptyShortTermMemory):
            raise ContractTransitionError("page one requires empty initial memory")
        if not isinstance(edit, AppendNewSectionMemoryEdit):
            raise ContractTransitionError(
                "page one must append the Active Reading Position section"
            )
    else:
        if not isinstance(current, ShortTermMemory):
            raise ContractTransitionError("later pages require initialized memory")
        if not isinstance(edit, ReplaceSectionMemoryEdit):
            raise ContractTransitionError(
                "every page after page one must replace Active Reading Position"
            )

    if edit.content is None:
        raise ContractTransitionError("a committed memory transition requires content")
    return ShortTermMemory.model_validate(
        {ACTIVE_READING_POSITION: edit.content},
    )


def append_document_structure_page(
    current: DocumentStructure,
    *,
    page_number: int,
    topics: Sequence[str],
) -> DocumentStructure:
    """Append exactly the next page while preserving supplied topic order."""

    _validate_page_number(page_number)
    expected = len(current.pages) + 1
    if page_number != expected:
        raise ContractTransitionError(
            f"expected document-structure page {expected}, got {page_number}"
        )
    page = DocumentStructurePage.model_validate(
        {"page_number": page_number, "topics": list(topics)},
    )
    return DocumentStructure(pages=[*current.pages, page])
