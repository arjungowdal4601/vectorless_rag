"""Persisted processing, usage, and sanitized error contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from .base import (
    NonBlankText,
    Sha256Hex,
    StrictContract,
    StrictNonNegativeInt,
    StrictPositiveInt,
)
from .enums import (
    JsonErrorCategory,
    JsonPageStatus,
    JsonRunStatus,
    PageStatus,
    RunStatus,
)


class ProcessingError(StrictContract):
    code: NonBlankText
    category: JsonErrorCategory
    message: NonBlankText
    retryable: bool


class ModelUsageRecord(StrictContract):
    page_number: StrictPositiveInt
    attempt_number: StrictPositiveInt
    model_id: NonBlankText
    provider_request_id: NonBlankText | None
    input_tokens: StrictNonNegativeInt
    output_tokens: StrictNonNegativeInt
    total_tokens: StrictNonNegativeInt
    latency_ms: StrictNonNegativeInt

    @model_validator(mode="after")
    def validate_total(self) -> ModelUsageRecord:
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens cannot be smaller than input plus output")
        return self


class PageProcessingState(StrictContract):
    page_number: StrictPositiveInt
    status: JsonPageStatus
    page_image_path: NonBlankText
    page_json_path: NonBlankText | None
    attempt_count: StrictNonNegativeInt
    error: ProcessingError | None

    @model_validator(mode="after")
    def validate_status_fields(self) -> PageProcessingState:
        expected_image = f"page_images/page-{self.page_number:04d}.png"
        if self.page_image_path != expected_image:
            raise ValueError(f"page_image_path must be {expected_image!r}")
        if self.status is PageStatus.COMPLETED:
            expected_json = f"page_artifacts/page-{self.page_number:04d}/page.json"
            if self.page_json_path != expected_json:
                raise ValueError(f"a completed page requires page_json_path {expected_json!r}")
            if self.error is not None:
                raise ValueError("a completed page cannot retain an error")
        elif self.page_json_path is not None:
            raise ValueError("an uncommitted page cannot reference page JSON")
        if self.status is PageStatus.FAILED and self.error is None:
            raise ValueError("a failed page requires an error")
        return self


class ProcessingManifest(StrictContract):
    schema_version: Literal["1"]
    document_id: Sha256Hex
    run_id: NonBlankText
    status: JsonRunStatus
    source_pdf_path: Literal["source/original.pdf"]
    source_sha256: Sha256Hex
    config_fingerprint: Sha256Hex
    page_count: StrictPositiveInt
    last_committed_page: StrictNonNegativeInt
    short_term_memory_path: Literal["final/short_term_memory.json"]
    document_structure_path: Literal["final/document_structure.json"]
    pages: list[PageProcessingState]
    error: ProcessingError | None

    @model_validator(mode="after")
    def validate_run_state(self) -> ProcessingManifest:
        if self.document_id != self.source_sha256:
            raise ValueError("document_id must equal the full source SHA-256")
        if len(self.pages) != self.page_count:
            raise ValueError("manifest must contain one state for every PDF page")
        numbers = [page.page_number for page in self.pages]
        if numbers != list(range(1, self.page_count + 1)):
            raise ValueError("manifest page states must be contiguous and ordered")

        completed_prefix = 0
        encountered_incomplete = False
        for page in self.pages:
            if page.status is PageStatus.COMPLETED:
                if encountered_incomplete:
                    raise ValueError("completed pages must form a contiguous prefix")
                completed_prefix += 1
            else:
                encountered_incomplete = True
        if self.last_committed_page != completed_prefix:
            raise ValueError("last_committed_page must equal the completed prefix")

        if self.status is RunStatus.COMPLETED:
            if completed_prefix != self.page_count:
                raise ValueError("a completed run requires every page to be completed")
            if self.error is not None:
                raise ValueError("a completed run cannot retain an error")
        if self.status is RunStatus.FAILED and self.error is None:
            raise ValueError("a failed run requires an error")
        return self
