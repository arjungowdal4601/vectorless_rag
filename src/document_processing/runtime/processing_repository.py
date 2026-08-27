"""Synchronous durable repository adapter used by ``DocumentProcessor``."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast
from uuid import uuid4

from document_processing.contracts import (
    DocumentStructure,
    EmptyShortTermMemory,
    PageProcessingState,
    PageStatus,
    ProcessingManifest,
    RunStatus,
    ShortTermMemory,
)
from document_processing.processing import (
    CompletionAudit,
    PageAttempt,
    PageCommit,
    ProcessingFailure,
    RecoveryCheckpoint,
    RunHandle,
)
from document_processing.processing.errors import FailureCode
from document_processing.processing.interfaces import PreparedPdf, StoredPdf
from document_processing.storage import PageCommitInput, RunRepository

_FAILURE_CLASSES = {
    FailureCode.CANCELLED: "cancelled",
    FailureCode.PDF_INTAKE_FAILED: "rendering",
    FailureCode.MODEL_TRANSIENT_EXHAUSTED: "transient_model",
    FailureCode.MODEL_CONTRACT_INVALID: "permanent_model",
    FailureCode.MODEL_PERMANENT: "permanent_model",
    FailureCode.STORAGE_FAILED: "storage",
    FailureCode.INTEGRITY_FAILED: "integrity",
    FailureCode.UNEXPECTED: "internal",
}


class DurableProcessingRepository:
    """Translate typed processor operations into atomic storage operations."""

    def __init__(
        self,
        repository: RunRepository,
        configuration: Mapping[str, object],
        configuration_fingerprint: str,
    ) -> None:
        self._repository = repository
        self._configuration = dict(configuration)
        self._fingerprint = configuration_fingerprint

    @property
    def storage(self) -> RunRepository:
        return self._repository

    def create_run(self) -> RunHandle:
        run_id = str(uuid4())
        row = self._repository.create_run(run_id, self._configuration)
        self._require_fingerprint(row)
        return self._handle(row)

    def load_run(self, run_id: str) -> RunHandle:
        row = self._repository.get_run(run_id)
        self._require_fingerprint(row)
        return self._handle(row)

    def record_source(self, run_id: str, stored: StoredPdf) -> None:
        row = self._repository.store_source(run_id, stored.source_path)
        if row["source_sha256"] != stored.document_id:
            raise ValueError("prepared document does not match the preserved source")

    def record_render_manifest(self, run_id: str, prepared: PreparedPdf) -> None:
        row = self._repository.get_run(run_id)
        if row["source_sha256"] != prepared.document_id:
            raise ValueError("rendered document does not match the journaled source")
        self._repository.set_render_manifest(run_id, prepared.manifest)

    def record_prepared_pdf(self, run_id: str, prepared: PreparedPdf) -> None:
        self.record_source(run_id, prepared)
        self.record_render_manifest(run_id, prepared)

    def initialize_run(self, run_id: str, *, page_count: int) -> RecoveryCheckpoint:
        row = self._repository.get_run(run_id)
        if row["total_pages"] != page_count:
            raise ValueError("rendered page count changed before initialization")
        self._repository.initialize_artifacts(
            run_id,
            EmptyShortTermMemory(),
            DocumentStructure(pages=[]),
        )
        return self._checkpoint(run_id)

    def recover_run(
        self,
        run_id: str,
        *,
        prepared: PreparedPdf,
    ) -> RecoveryCheckpoint:
        row = self._repository.get_run(run_id)
        if row["source_sha256"] != prepared.document_id:
            raise ValueError("prepared document identity differs from the durable run")
        if row["total_pages"] != prepared.manifest.page_count:
            raise ValueError("prepared page count differs from the durable run")
        recovery = self._repository.recover_run(run_id)
        recovery.audit.require_ok()
        first = recovery.audit.first_incomplete_page
        if first is not None:
            page = self._repository.get_page(run_id, first)
            if page["status"] == "skipped":
                raise ValueError("skipped pages cannot be resumed")
            if page["status"] == "failed":
                self._repository.requeue_failed_page(run_id, first)
        return self._checkpoint(run_id)

    def mark_run_running(self, run_id: str, *, resuming: bool) -> None:
        self._repository.mark_run_running(run_id, resuming=resuming)

    def is_cancel_requested(self, run_id: str) -> bool:
        return self._repository.is_cancel_requested(run_id)

    def begin_page_attempt(self, run_id: str, *, page_number: int) -> PageAttempt:
        attempt_id = self._repository.begin_page_attempt(run_id, page_number)
        row = self._repository.get_page(run_id, page_number)
        return PageAttempt(attempt_id, cast(int, row["attempt_count"]))

    def fail_page_attempt(
        self,
        run_id: str,
        *,
        attempt_id: str,
        failure: object,
        will_retry: bool,
    ) -> None:
        typed = self._failure(failure)
        self._repository.fail_page_attempt(
            run_id,
            attempt_id,
            code=typed.code.value,
            detail=typed.message,
            failure_class=_FAILURE_CLASSES[typed.code],
            will_retry=will_retry,
        )

    def discard_page_attempt(
        self,
        run_id: str,
        *,
        attempt_id: str,
        reason: str,
    ) -> None:
        self._repository.discard_page_attempt(run_id, attempt_id, reason)

    def commit_page(
        self,
        commit: PageCommit,
        *,
        cancel_check: Callable[[], bool],
    ) -> None:
        self._repository.commit_page(
            PageCommitInput(
                run_id=commit.run_id,
                page_number=commit.page_number,
                attempt_id=commit.attempt_id,
                page=commit.page_artifact.model_dump(mode="json"),
                short_term_memory=commit.short_term_memory.model_dump(mode="json", by_alias=True),
                document_structure=commit.document_structure.model_dump(mode="json"),
                model_response=commit.model_response.model_dump(mode="json"),
                usage=commit.usage.model_dump(mode="json"),
                cancel_check=cancel_check,
            )
        )

    def fail_run(self, run_id: str, failure: object) -> None:
        typed = self._failure(failure)
        current = self._repository.get_run(run_id)
        if current["status"] == "completed":
            return
        self._repository.mark_run_failed(
            run_id,
            typed.code.value,
            typed.message,
            failure_class=_FAILURE_CLASSES[typed.code],
        )

    def audit_completion(
        self,
        run_id: str,
        *,
        prepared: PreparedPdf,
    ) -> CompletionAudit:
        report = self._repository.audit_run(run_id)
        issues = tuple(f"{issue.code}: {issue.message}" for issue in report.issues)
        row = self._repository.get_run(run_id)
        pages = self._repository.get_pages(run_id)
        complete = (
            report.ok
            and row["source_sha256"] == prepared.document_id
            and row["total_pages"] == prepared.manifest.page_count
            and report.head_page == row["total_pages"]
            and all(page["status"] == "completed" for page in pages)
        )
        published = row["status"] == "completed" and bool(row["final_manifest_path"])
        if not complete:
            return CompletionAudit(False, None, issues or ("run is incomplete",), published)
        manifest = ProcessingManifest(
            schema_version="1",
            document_id=cast(str, row["source_sha256"]),
            run_id=run_id,
            status=RunStatus.COMPLETED,
            source_pdf_path="source/original.pdf",
            source_sha256=cast(str, row["source_sha256"]),
            config_fingerprint=cast(str, row["config_sha256"]),
            page_count=cast(int, row["total_pages"]),
            last_committed_page=cast(int, row["head_page"]),
            short_term_memory_path="final/short_term_memory.json",
            document_structure_path="final/document_structure.json",
            pages=[self._completed_page(page) for page in pages],
            error=None,
        )
        return CompletionAudit(True, manifest, published=published)

    def finalize_completion(
        self,
        run_id: str,
        *,
        audit: CompletionAudit,
        cancel_check: Callable[[], bool],
    ) -> ProcessingManifest:
        if not audit.complete or audit.manifest is None:
            raise ValueError("completion requires a successful audit")
        final = self._repository.prepare_final_manifest(run_id, audit.manifest)
        self._repository.finalize_run(final, cancel_check=cancel_check)
        self._repository.audit_run(run_id).require_ok()
        return audit.manifest

    def _checkpoint(self, run_id: str) -> RecoveryCheckpoint:
        value = self._repository.load_checkpoint(run_id)
        completed = cast(int, value["head_page"])
        memory_value = value["short_term_memory"]
        memory = (
            EmptyShortTermMemory.model_validate(memory_value)
            if completed == 0
            else ShortTermMemory.model_validate(memory_value)
        )
        structure = DocumentStructure.model_validate(value["document_structure"])
        first_incomplete = cast(int | None, value["first_incomplete_page"])
        next_attempt = 1
        if first_incomplete is not None:
            page = self._repository.get_page(run_id, first_incomplete)
            next_attempt = cast(int, page["attempt_count"]) + 1
        return RecoveryCheckpoint(completed, memory, structure, next_attempt)

    def _handle(self, row: Mapping[str, Any]) -> RunHandle:
        run_id = cast(str, row["run_id"])
        return RunHandle(
            run_id=run_id,
            run_directory=self._repository.paths_for_run(run_id).root,
            document_id=cast(str | None, row["source_sha256"]),
            rendered=row["render_manifest_sha256"] is not None,
            initialized=row["head_commit_id"] is not None,
            status=cast(str, row["status"]),
            failure=self._row_failure(row),
            completed_pages=cast(int, row["head_page"]),
            total_pages=cast(int | None, row["total_pages"]),
        )

    @staticmethod
    def _row_failure(row: Mapping[str, Any]) -> ProcessingFailure | None:
        if row["status"] != "failed" or not row["failure_code"]:
            return None
        try:
            code = FailureCode(cast(str, row["failure_code"]))
        except ValueError:
            code = FailureCode.UNEXPECTED
        return ProcessingFailure(
            code=code,
            message=cast(str, row["failure_detail"] or "Document processing failed."),
            retryable=row["failure_class"] == "transient_model",
        )

    def _require_fingerprint(self, row: Mapping[str, Any]) -> None:
        if row["config_sha256"] != self._fingerprint:
            raise ValueError("run configuration fingerprint does not match this processor")

    @staticmethod
    def _failure(value: object) -> ProcessingFailure:
        if not isinstance(value, ProcessingFailure):
            raise TypeError("processor repository requires a ProcessingFailure")
        return value

    @staticmethod
    def _completed_page(row: Mapping[str, Any]) -> PageProcessingState:
        return PageProcessingState(
            page_number=cast(int, row["page_number"]),
            status=PageStatus.COMPLETED,
            page_image_path=cast(str, row["image_path"]),
            page_json_path=cast(str, row["page_json_path"]),
            attempt_count=cast(int, row["attempt_count"]),
            error=None,
        )
