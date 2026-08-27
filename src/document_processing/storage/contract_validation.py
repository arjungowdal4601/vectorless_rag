"""Validation adapter for the public document-processing contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from document_processing.contracts import (
    DocumentStructure,
    EmptyShortTermMemory,
    ModelResponse,
    ModelUsageRecord,
    PageArtifact,
    ShortTermMemory,
    append_document_structure_page,
    apply_short_term_memory_edit,
    build_page_artifact,
)

from .files import canonical_json, jsonable


@dataclass(frozen=True)
class ValidatedPageCommit:
    page: PageArtifact
    short_term_memory: ShortTermMemory
    document_structure: DocumentStructure
    model_response: ModelResponse
    usage: ModelUsageRecord


def validate_initial(
    short_term_memory: Any, document_structure: Any
) -> tuple[EmptyShortTermMemory, DocumentStructure]:
    memory = EmptyShortTermMemory.model_validate(jsonable(short_term_memory))
    structure = DocumentStructure.model_validate(jsonable(document_structure))
    if structure.pages:
        raise ValueError("initial document structure must be empty")
    return memory, structure


def validate_page_commit(
    *,
    page_number: int,
    image_path: str,
    page: Any,
    short_term_memory: Any,
    document_structure: Any,
    model_response: Any,
    usage: Any,
    prior_short_term_memory: Any,
    prior_document_structure: Any,
) -> ValidatedPageCommit:
    artifact = PageArtifact.model_validate(jsonable(page))
    if artifact.page_number != page_number or artifact.page_image_path != image_path:
        raise ValueError("page artifact does not match the durable page record")
    memory = ShortTermMemory.model_validate(jsonable(short_term_memory))
    structure = DocumentStructure.model_validate(jsonable(document_structure))
    response = ModelResponse.model_validate(jsonable(model_response))
    prior_memory = _validate_prior_memory(page_number, prior_short_term_memory)
    prior_structure = _validate_prior_structure(page_number, prior_document_structure)
    expected_page, expected_memory, expected_structure = _validate_derivation(
        page_number=page_number,
        image_path=image_path,
        artifact=artifact,
        memory=memory,
        structure=structure,
        response=response,
        prior_memory=prior_memory,
        prior_structure=prior_structure,
    )
    usage_record = ModelUsageRecord.model_validate(jsonable(usage))
    if usage_record.page_number != page_number:
        raise ValueError("model usage page number does not match the committed page")
    return ValidatedPageCommit(
        page=expected_page,
        short_term_memory=expected_memory,
        document_structure=expected_structure,
        model_response=response,
        usage=usage_record,
    )


def validate_initial_json(memory: bytes, structure: bytes) -> None:
    EmptyShortTermMemory.model_validate_json(memory)
    value = DocumentStructure.model_validate_json(structure)
    if value.pages:
        raise ValueError("initial document structure must be empty")


def validate_prior_state_json(
    *, page_number: int, memory: bytes, structure: bytes
) -> tuple[EmptyShortTermMemory | ShortTermMemory, DocumentStructure]:
    prior_memory: EmptyShortTermMemory | ShortTermMemory
    if page_number == 1:
        prior_memory = EmptyShortTermMemory.model_validate_json(memory)
    else:
        prior_memory = ShortTermMemory.model_validate_json(memory)
    prior_structure = DocumentStructure.model_validate_json(structure)
    _require_prior_prefix(page_number, prior_structure)
    return prior_memory, prior_structure


def validate_page_json(
    *,
    page_number: int,
    image_path: str,
    page: bytes,
    memory: bytes,
    structure: bytes,
    model_response: bytes,
    usage: bytes,
    prior_memory: bytes,
    prior_structure: bytes,
) -> None:
    artifact = PageArtifact.model_validate_json(page)
    if artifact.page_number != page_number or artifact.page_image_path != image_path:
        raise ValueError("stored page artifact does not match the page record")
    memory_value = ShortTermMemory.model_validate_json(memory)
    structure_value = DocumentStructure.model_validate_json(structure)
    response = ModelResponse.model_validate_json(model_response)
    prior_memory_value, prior_structure_value = validate_prior_state_json(
        page_number=page_number,
        memory=prior_memory,
        structure=prior_structure,
    )
    _validate_derivation(
        page_number=page_number,
        image_path=image_path,
        artifact=artifact,
        memory=memory_value,
        structure=structure_value,
        response=response,
        prior_memory=prior_memory_value,
        prior_structure=prior_structure_value,
    )
    usage_value = ModelUsageRecord.model_validate_json(usage)
    if usage_value.page_number != page_number:
        raise ValueError("stored usage has the wrong page number")


def _validate_prior_memory(page_number: int, value: Any) -> EmptyShortTermMemory | ShortTermMemory:
    if page_number == 1:
        return EmptyShortTermMemory.model_validate(jsonable(value))
    return ShortTermMemory.model_validate(jsonable(value))


def _validate_prior_structure(page_number: int, value: Any) -> DocumentStructure:
    structure = DocumentStructure.model_validate(jsonable(value))
    _require_prior_prefix(page_number, structure)
    return structure


def _require_prior_prefix(page_number: int, structure: DocumentStructure) -> None:
    if len(structure.pages) != page_number - 1:
        raise ValueError("prior document structure does not match the committed prefix")


def _validate_derivation(
    *,
    page_number: int,
    image_path: str,
    artifact: PageArtifact,
    memory: ShortTermMemory,
    structure: DocumentStructure,
    response: ModelResponse,
    prior_memory: EmptyShortTermMemory | ShortTermMemory,
    prior_structure: DocumentStructure,
) -> tuple[PageArtifact, ShortTermMemory, DocumentStructure]:
    expected_page = build_page_artifact(
        response.page_output,
        page_number=page_number,
        page_image_path=image_path,
    )
    expected_memory = apply_short_term_memory_edit(
        prior_memory,
        response.memory_edits.short_term_memory_edits[0],
        page_number=page_number,
    )
    expected_structure = append_document_structure_page(
        prior_structure,
        page_number=page_number,
        topics=response.memory_edits.document_structure_edits.topics,
    )
    if canonical_json(artifact) != canonical_json(expected_page):
        raise ValueError("page artifact is not the deterministic model-response projection")
    if canonical_json(memory) != canonical_json(expected_memory):
        raise ValueError("short-term memory is not the deterministic state transition")
    if canonical_json(structure) != canonical_json(expected_structure):
        raise ValueError("document structure does not preserve and extend the prior prefix")
    return expected_page, expected_memory, expected_structure
