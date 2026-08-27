"""Strict contract factories for processing orchestration tests."""

from __future__ import annotations

from document_processing.analysis import AnalysisResult
from document_processing.contracts import (
    DocumentStructure,
    ModelResponse,
    ModelUsageRecord,
    PageProcessingState,
    PageStatus,
    ProcessingManifest,
    RunStatus,
    ShortTermMemory,
)
from document_processing.processing import RecoveryCheckpoint


def model_response(page_number: int, *, valid_transition: bool = True) -> ModelResponse:
    edit_type = "append_new_section" if page_number == 1 else "replace_section"
    if not valid_transition:
        edit_type = "replace_section" if page_number == 1 else "append_new_section"
    return ModelResponse.model_validate(
        {
            "memory_edits": {
                "short_term_memory_edits": [
                    {
                        "edit_type": edit_type,
                        "section_heading": "Active Reading Position",
                        "content": {
                            "parent_section": ["Guide"],
                            "current_subsection": f"Section {page_number}",
                            "last_visible_clause": f"Clause {page_number}",
                            "current_topic_flow": f"Topic {page_number}",
                            "unfinished_content": None,
                            "next_page_inspection": "Check continuation",
                            "document_completion": "In progress",
                        },
                    }
                ],
                "document_structure_edits": {"topics": [f"Section {page_number}"]},
            },
            "page_output": {
                "page_type": "body_content",
                "index_decision": "index_worthy",
                "index_reason": "Visible substantive guidance.",
                "summary": f"Page {page_number} guidance.",
                "topics": [
                    {
                        "topic_name": f"Topic {page_number}",
                        "topic_description": f"Details on topic {page_number}.",
                    }
                ],
                "assets": [
                    {
                        "asset_type": "formula",
                        "asset_name": "",
                        "asset_description": f"Formula on page {page_number}.",
                    }
                ],
            },
        }
    )


def analysis_result(page_number: int, *, attempt: int = 1) -> AnalysisResult:
    usage = ModelUsageRecord(
        page_number=page_number,
        attempt_number=attempt,
        model_id="gpt-5.6-luna",
        provider_request_id=None,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        latency_ms=20,
    )
    return AnalysisResult(model_response(page_number), usage)


def resumed_checkpoint() -> RecoveryCheckpoint:
    first = model_response(1)
    edit = first.memory_edits.short_term_memory_edits[0]
    assert edit.content is not None
    memory = ShortTermMemory.model_validate({"Active Reading Position": edit.content})
    structure = DocumentStructure.model_validate(
        {"pages": [{"page_number": 1, "topics": ["Section 1"]}]}
    )
    return RecoveryCheckpoint(1, memory, structure)


def completed_manifest(
    *,
    document_id: str,
    config_fingerprint: str,
    run_id: str,
    page_count: int,
    attempt_counts: dict[int, int],
) -> ProcessingManifest:
    pages = [
        PageProcessingState(
            page_number=number,
            status=PageStatus.COMPLETED,
            page_image_path=f"page_images/page-{number:04d}.png",
            page_json_path=f"page_artifacts/page-{number:04d}/page.json",
            attempt_count=attempt_counts.get(number, 1),
            error=None,
        )
        for number in range(1, page_count + 1)
    ]
    return ProcessingManifest(
        schema_version="1",
        document_id=document_id,
        run_id=run_id,
        status=RunStatus.COMPLETED,
        source_pdf_path="source/original.pdf",
        source_sha256=document_id,
        config_fingerprint=config_fingerprint,
        page_count=page_count,
        last_committed_page=page_count,
        short_term_memory_path="final/short_term_memory.json",
        document_structure_path="final/document_structure.json",
        pages=pages,
        error=None,
    )
