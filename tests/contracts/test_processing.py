"""Offline tests for status, manifest, usage, and error contracts."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from document_processing.contracts import (
    ErrorCategory,
    ModelUsageRecord,
    PageProcessingState,
    PageStatus,
    ProcessingError,
    ProcessingManifest,
    RunStatus,
)

HASH = "a" * 64


def cancelled_error() -> ProcessingError:
    return ProcessingError(
        code="cancelled",
        category=ErrorCategory.CANCELLED,
        message="Processing was cancelled.",
        retryable=False,
    )


def page_state(
    number: int,
    status: PageStatus,
) -> PageProcessingState:
    completed = status is PageStatus.COMPLETED
    failed = status is PageStatus.FAILED
    return PageProcessingState(
        page_number=number,
        status=status,
        page_image_path=f"page_images/page-{number:04d}.png",
        page_json_path=f"page_artifacts/page-{number:04d}/page.json" if completed else None,
        attempt_count=1 if completed or failed else 0,
        error=cancelled_error() if failed else None,
    )


def manifest(
    *,
    status: RunStatus,
    pages: list[PageProcessingState],
    last_committed_page: int,
    error: ProcessingError | None = None,
) -> ProcessingManifest:
    return ProcessingManifest(
        schema_version="1",
        document_id=HASH,
        run_id="run-1",
        status=status,
        source_pdf_path="source/original.pdf",
        source_sha256=HASH,
        config_fingerprint=HASH,
        page_count=len(pages),
        last_committed_page=last_committed_page,
        short_term_memory_path="final/short_term_memory.json",
        document_structure_path="final/document_structure.json",
        pages=pages,
        error=error,
    )


class ProcessingContractTests(unittest.TestCase):
    def test_status_and_error_category_do_not_coerce_bytes(self) -> None:
        state = page_state(1, PageStatus.PENDING).model_dump(mode="json")
        state["status"] = b"pending"
        with self.assertRaises(ValidationError):
            PageProcessingState.model_validate(state)

        error = cancelled_error().model_dump(mode="json")
        error["category"] = b"cancelled"
        with self.assertRaises(ValidationError):
            ProcessingError.model_validate(error)

    def test_document_id_must_be_the_full_source_sha256(self) -> None:
        value = manifest(
            status=RunStatus.RUNNING,
            pages=[page_state(1, PageStatus.PENDING)],
            last_committed_page=0,
        ).model_dump(mode="json")
        value["document_id"] = "not-a-sha256"
        with self.assertRaises(ValidationError):
            ProcessingManifest.model_validate(value)

        value["document_id"] = "b" * 64
        with self.assertRaises(ValidationError):
            ProcessingManifest.model_validate(value)

    def test_manifest_paths_and_schema_are_exact(self) -> None:
        value = manifest(
            status=RunStatus.RUNNING,
            pages=[page_state(1, PageStatus.PENDING)],
            last_committed_page=0,
        ).model_dump(mode="json")
        for field, replacement in (
            ("schema_version", "2"),
            ("source_pdf_path", "../original.pdf"),
            ("short_term_memory_path", "short_term_memory.json"),
            ("document_structure_path", "document_structure.json"),
        ):
            invalid = dict(value)
            invalid[field] = replacement
            with self.subTest(field=field), self.assertRaises(ValidationError):
                ProcessingManifest.model_validate(invalid)

        invalid_page = dict(value)
        invalid_page["pages"] = [
            {**value["pages"][0], "page_image_path": "page_images/page-9999.png"}
        ]
        with self.assertRaises(ValidationError):
            ProcessingManifest.model_validate(invalid_page)

    def test_completed_run_requires_every_page(self) -> None:
        result = manifest(
            status=RunStatus.COMPLETED,
            pages=[
                page_state(1, PageStatus.COMPLETED),
                page_state(2, PageStatus.COMPLETED),
            ],
            last_committed_page=2,
        )
        self.assertEqual(result.status, RunStatus.COMPLETED)

        with self.assertRaises(ValidationError):
            manifest(
                status=RunStatus.COMPLETED,
                pages=[
                    page_state(1, PageStatus.COMPLETED),
                    page_state(2, PageStatus.SKIPPED),
                ],
                last_committed_page=1,
            )

    def test_completed_pages_must_form_prefix(self) -> None:
        with self.assertRaises(ValidationError):
            manifest(
                status=RunStatus.RUNNING,
                pages=[
                    page_state(1, PageStatus.COMPLETED),
                    page_state(2, PageStatus.PENDING),
                    page_state(3, PageStatus.COMPLETED),
                ],
                last_committed_page=1,
            )

    def test_last_committed_page_must_match_prefix(self) -> None:
        with self.assertRaises(ValidationError):
            manifest(
                status=RunStatus.RUNNING,
                pages=[page_state(1, PageStatus.COMPLETED)],
                last_committed_page=0,
            )

    def test_cancellation_uses_failed_plus_error(self) -> None:
        result = manifest(
            status=RunStatus.FAILED,
            pages=[page_state(1, PageStatus.PENDING)],
            last_committed_page=0,
            error=cancelled_error(),
        )
        self.assertEqual(result.status, RunStatus.FAILED)
        assert result.error is not None
        self.assertEqual(result.error.code, "cancelled")

    def test_failed_page_and_run_require_error(self) -> None:
        with self.assertRaises(ValidationError):
            PageProcessingState(
                page_number=1,
                status=PageStatus.FAILED,
                page_image_path="page_images/page-0001.png",
                page_json_path=None,
                attempt_count=1,
                error=None,
            )
        with self.assertRaises(ValidationError):
            manifest(
                status=RunStatus.FAILED,
                pages=[page_state(1, PageStatus.PENDING)],
                last_committed_page=0,
            )

    def test_usage_record_is_strict_and_consistent(self) -> None:
        usage = ModelUsageRecord(
            page_number=1,
            attempt_number=1,
            model_id="provider:model",
            provider_request_id=None,
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            latency_ms=500,
        )
        self.assertEqual(usage.total_tokens, 120)

        with self.assertRaises(ValidationError):
            ModelUsageRecord(
                page_number=1,
                attempt_number=1,
                model_id="provider:model",
                provider_request_id=None,
                input_tokens=100,
                output_tokens=20,
                total_tokens=119,
                latency_ms=500,
            )
        with self.assertRaises(ValidationError):
            ModelUsageRecord.model_validate(
                {
                    "page_number": True,
                    "attempt_number": 1,
                    "model_id": "provider:model",
                    "provider_request_id": None,
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                    "latency_ms": 1,
                }
            )


if __name__ == "__main__":
    unittest.main()
