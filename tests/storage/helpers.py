"""Deterministic fixtures for storage tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from document_processing.pdf import PdfIntakeService, PdfRenderConfig
from document_processing.storage import PageCommitInput, RunRepository
from tests.pdf.helpers import PageSpec, make_pdf

POSITION = {
    "parent_section": None,
    "current_subsection": "Overview",
    "last_visible_clause": "Visible clause.",
    "current_topic_flow": "Test topic",
    "unfinished_content": None,
    "next_page_inspection": None,
    "document_completion": "In progress",
}
POSITION_2 = {
    **POSITION,
    "current_subsection": "Details",
    "last_visible_clause": "Second visible clause.",
    "current_topic_flow": "Detailed topic",
}


def make_repository(tmp_path: Path, *, run_id: str = "run-1", page_count: int = 1) -> RunRepository:
    repository = RunRepository(tmp_path / "var" / "document-processing")
    repository.initialize()
    repository.create_run(run_id, {"model": "test:model", "schema_version": 1})
    upload = tmp_path / "upload.pdf"
    paths = repository.paths_for_run(run_id)
    make_pdf(upload, tuple(PageSpec() for _ in range(page_count)))
    prepared = PdfIntakeService(PdfRenderConfig()).prepare_path(upload, paths.root)
    repository.store_source(run_id, prepared.source_path, original_filename="fixture.pdf")
    repository.set_render_manifest(run_id, prepared.manifest)
    repository.initialize_artifacts(run_id, {}, {"pages": []})
    repository.mark_run_running(run_id)
    return repository


def model_response() -> dict[str, Any]:
    return {
        "memory_edits": {
            "short_term_memory_edits": [
                {
                    "edit_type": "append_new_section",
                    "section_heading": "Active Reading Position",
                    "content": POSITION,
                }
            ],
            "document_structure_edits": {"topics": ["Overview"]},
        },
        "page_output": {
            "page_type": "body_content",
            "index_decision": "index_worthy",
            "index_reason": "The visible page contains substantive test material.",
            "summary": "Overview of the substantive test material.",
            "topics": [
                {
                    "topic_name": "Overview",
                    "topic_description": "The visible substantive test material.",
                }
            ],
            "assets": [],
        },
    }


def page_artifact() -> dict[str, Any]:
    return {
        "page_number": 1,
        "page_type": "body_content",
        "page_image_path": "page_images/page-0001.png",
        "index_decision": "index_worthy",
        "index_reason": "The visible page contains substantive test material.",
        "summary": "Overview of the substantive test material.",
        "topics": [
            {
                "topic_id": "p0001-t001",
                "topic_name": "Overview",
                "topic_description": "The visible substantive test material.",
            }
        ],
        "assets": [],
    }


def second_model_response() -> dict[str, Any]:
    return {
        "memory_edits": {
            "short_term_memory_edits": [
                {
                    "edit_type": "replace_section",
                    "section_heading": "Active Reading Position",
                    "content": POSITION_2,
                }
            ],
            "document_structure_edits": {"topics": ["Details"]},
        },
        "page_output": {
            "page_type": "body_content",
            "index_decision": "index_worthy",
            "index_reason": "The second page contains detailed test material.",
            "summary": "Details of the substantive test material.",
            "topics": [
                {
                    "topic_name": "Details",
                    "topic_description": "Detailed visible test material.",
                }
            ],
            "assets": [],
        },
    }


def second_page_artifact() -> dict[str, Any]:
    return {
        "page_number": 2,
        "page_type": "body_content",
        "page_image_path": "page_images/page-0002.png",
        "index_decision": "index_worthy",
        "index_reason": "The second page contains detailed test material.",
        "summary": "Details of the substantive test material.",
        "topics": [
            {
                "topic_id": "p0002-t001",
                "topic_name": "Details",
                "topic_description": "Detailed visible test material.",
            }
        ],
        "assets": [],
    }


def usage() -> dict[str, Any]:
    return {
        "page_number": 1,
        "attempt_number": 1,
        "model_id": "test:model",
        "provider_request_id": "request-1",
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "latency_ms": 20,
    }


def second_usage() -> dict[str, Any]:
    return {
        **usage(),
        "page_number": 2,
        "provider_request_id": "request-2",
    }


def commit_input(
    repository: RunRepository,
    *,
    fault_hook: Callable[[str], None] | None = None,
) -> PageCommitInput:
    attempt = repository.begin_page_attempt(
        "run-1", 1, model_id="test:model", request_fingerprint="fixture"
    )
    return PageCommitInput(
        run_id="run-1",
        page_number=1,
        attempt_id=attempt,
        page=page_artifact(),
        short_term_memory={"Active Reading Position": POSITION},
        document_structure={"pages": [{"page_number": 1, "topics": ["Overview"]}]},
        model_response=model_response(),
        usage=usage(),
        fault_hook=fault_hook,
    )


def second_commit_input(repository: RunRepository) -> PageCommitInput:
    attempt = repository.begin_page_attempt(
        "run-1", 2, model_id="test:model", request_fingerprint="fixture-2"
    )
    return PageCommitInput(
        run_id="run-1",
        page_number=2,
        attempt_id=attempt,
        page=second_page_artifact(),
        short_term_memory={"Active Reading Position": POSITION_2},
        document_structure={
            "pages": [
                {"page_number": 1, "topics": ["Overview"]},
                {"page_number": 2, "topics": ["Details"]},
            ]
        },
        model_response=second_model_response(),
        usage=second_usage(),
    )


def completed_manifest(repository: RunRepository) -> dict[str, Any]:
    run = repository.get_run("run-1")
    page = repository.get_pages("run-1")[0]
    return {
        "schema_version": "1",
        "document_id": run["source_sha256"],
        "run_id": "run-1",
        "status": "completed",
        "source_pdf_path": "source/original.pdf",
        "source_sha256": run["source_sha256"],
        "config_fingerprint": run["config_sha256"],
        "page_count": 1,
        "last_committed_page": 1,
        "short_term_memory_path": "final/short_term_memory.json",
        "document_structure_path": "final/document_structure.json",
        "pages": [
            {
                "page_number": 1,
                "status": "completed",
                "page_image_path": page["image_path"],
                "page_json_path": page["page_json_path"],
                "attempt_count": 1,
                "error": None,
            }
        ],
        "error": None,
    }
