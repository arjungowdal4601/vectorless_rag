"""Authoritative recovery after an uncertain durable commit boundary."""

from __future__ import annotations

from document_processing.contracts import DocumentStructure, ShortTermMemory
from document_processing.processing.interfaces import PreparedPdf, ProcessingRepository
from document_processing.processing.offload import BlockingCallTracker
from document_processing.processing.state import validate_checkpoint


async def page_commit_is_durable(
    repository: ProcessingRepository,
    offloads: BlockingCallTracker,
    prepared: PreparedPdf,
    *,
    run_id: str,
    page_number: int,
    short_term_memory: ShortTermMemory,
    document_structure: DocumentStructure,
) -> bool:
    """Recover and distinguish the previous checkpoint from the committed page."""

    checkpoint = await offloads.run(repository.recover_run, run_id, prepared=prepared)
    validate_checkpoint(
        completed_through=checkpoint.completed_through,
        total_pages=prepared.manifest.page_count,
        short_term_memory=checkpoint.short_term_memory,
        document_structure=checkpoint.document_structure,
        next_attempt_number=checkpoint.next_attempt_number,
    )
    if checkpoint.completed_through == page_number - 1:
        return False
    if checkpoint.completed_through != page_number:
        raise ValueError("commit recovery returned an impossible page head")
    if checkpoint.short_term_memory != short_term_memory:
        raise ValueError("committed memory differs from the submitted checkpoint")
    if checkpoint.document_structure != document_structure:
        raise ValueError("committed structure differs from the submitted checkpoint")
    return True
