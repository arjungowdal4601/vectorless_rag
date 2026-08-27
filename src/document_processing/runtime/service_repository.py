"""Async local-service adapter over the shared durable run journal."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from document_processing.contracts import PageStatus, ProcessingManifest, RunStatus
from document_processing.errors import InvalidRunOperationError, RunNotFoundError
from document_processing.processing.offload import BlockingCallTracker
from document_processing.service.models import (
    FailureView,
    PagePage,
    PageView,
    ProgressView,
    RecoveryItem,
    RunView,
    Submission,
)
from document_processing.storage import (
    IntegrityError,
    InvalidTransition,
    RunNotFound,
    RunRepository,
)

from .startup import recover_durable_runs

_CATEGORY_MAP = {
    "cancelled": "cancelled",
    "rendering": "rendering",
    "transient_model": "transient_model",
    "permanent_model": "permanent_model",
    "permanent": "permanent_model",
    "storage": "storage",
    "integrity": "integrity",
    "input": "input",
    "internal": "internal",
}

_SAFE_MESSAGES = {
    "cancelled": "Document processing was cancelled.",
    "rendering": "The PDF could not be rendered for processing.",
    "transient_model": "A temporary model failure persisted after all attempts.",
    "permanent_model": "Page analysis failed its required contract.",
    "storage": "A durable storage operation failed.",
    "integrity": "The run failed its integrity audit.",
    "input": "The source document could not be processed.",
    "internal": "Document processing failed.",
}


class DurableRunServiceRepository:
    """Expose durable run state without blocking the FastAPI event loop."""

    def __init__(
        self,
        repository: RunRepository,
        configuration_fingerprint: str,
        max_model_attempts: int,
        offload_tracker: BlockingCallTracker | None = None,
    ) -> None:
        self._repository = repository
        self._fingerprint = configuration_fingerprint
        self._max_model_attempts = max_model_attempts
        self._offloads = offload_tracker or BlockingCallTracker()

    async def register_submission(self, submission: Submission) -> RunView:
        return await self._translate(
            submission.run_id,
            self._register_submission,
            submission,
        )

    async def get_run(self, run_id: str) -> RunView:
        return await self._translate(run_id, self._get_run, run_id)

    async def list_pages(self, run_id: str, *, offset: int, limit: int) -> PagePage:
        return await self._translate(run_id, self._list_pages, run_id, offset, limit)

    async def request_cancel(self, run_id: str) -> RunView:
        return await self._translate(run_id, self._request_cancel, run_id)

    async def prepare_resume(self, run_id: str) -> tuple[RunView, Path]:
        return await self._translate(run_id, self._prepare_resume, run_id)

    async def fail_run(self, run_id: str, failure: FailureView) -> RunView:
        return await self._translate(run_id, self._fail_run, run_id, failure)

    async def get_manifest(self, run_id: str) -> Mapping[str, Any]:
        return await self._translate(run_id, self._get_manifest, run_id)

    async def recover_startup(
        self,
        configuration_fingerprint: str,
    ) -> Sequence[RecoveryItem]:
        if configuration_fingerprint != self._fingerprint:
            raise RuntimeError("startup configuration fingerprint is inconsistent")
        return await self._offloads.run(self._recover_startup)

    async def drain_offloads(self) -> None:
        await self._offloads.drain()

    def _register_submission(self, submission: Submission) -> RunView:
        if submission.configuration_fingerprint != self._fingerprint:
            raise InvalidRunOperationError(
                "The submission configuration does not match this service.",
                run_id=submission.run_id,
            )
        row = self._repository.create_run(submission.run_id, submission.configuration)
        if row["config_sha256"] != submission.configuration_fingerprint:
            raise InvalidRunOperationError(
                "The durable configuration fingerprint is inconsistent.",
                run_id=submission.run_id,
            )
        if submission.source_path.stat().st_size != submission.size_bytes:
            raise InvalidRunOperationError(
                "The preserved upload size changed before registration.",
                run_id=submission.run_id,
            )
        row = self._repository.store_source(
            submission.run_id,
            submission.source_path,
            original_filename=submission.filename,
        )
        if row["source_sha256"] != submission.document_id:
            raise InvalidRunOperationError(
                "The preserved upload identity changed before registration.",
                run_id=submission.run_id,
            )
        return self._run_view(row)

    def _get_run(self, run_id: str) -> RunView:
        return self._run_view(self._repository.get_run(run_id))

    def _list_pages(self, run_id: str, offset: int, limit: int) -> PagePage:
        rows = self._repository.get_pages(run_id)
        selected = rows[offset : offset + limit]
        return PagePage(
            items=tuple(self._page_view(row) for row in selected),
            offset=offset,
            limit=limit,
            total=len(rows),
        )

    def _request_cancel(self, run_id: str) -> RunView:
        row = self._repository.request_cancel(run_id)
        return self._run_view(row)

    def _prepare_resume(self, run_id: str) -> tuple[RunView, Path]:
        row = self._repository.get_run(run_id)
        if row["status"] != "failed":
            raise InvalidRunOperationError("Only a failed run can be resumed.", run_id=run_id)
        if row["config_sha256"] != self._fingerprint:
            raise InvalidRunOperationError(
                "The run configuration differs from the current service.", run_id=run_id
            )
        if row["failure_class"] == "integrity":
            raise InvalidRunOperationError(
                "A run with corrupt referenced state cannot be resumed.", run_id=run_id
            )
        self._repository.mark_run_running(run_id, resuming=True)
        try:
            recovery = self._repository.recover_run(run_id)
            if not recovery.audit.ok:
                raise InvalidRunOperationError(
                    "The run failed its resume integrity audit.", run_id=run_id
                )
            current = self._repository.get_run(run_id)
            if current["status"] != "resuming":
                raise InvalidTransition("the resume claim was cancelled or superseded")
            if not self._has_attempt_budget(run_id):
                self._repository.fail_resuming_run(
                    run_id,
                    "model_transient_exhausted",
                    "The page exhausted its durable model-attempt budget.",
                    failure_class="transient_model",
                )
                raise InvalidRunOperationError(
                    "The first incomplete page cannot be resumed.", run_id=run_id
                )
            first = recovery.audit.first_incomplete_page
            if first is not None:
                page = self._repository.get_page(run_id, first)
                if page["status"] == "skipped":
                    self._repository.fail_resuming_run(
                        run_id,
                        "reserved_skip",
                        "Runs containing skipped pages cannot be resumed.",
                        failure_class="permanent_model",
                    )
                    raise InvalidRunOperationError(
                        "Runs containing skipped pages cannot be resumed.", run_id=run_id
                    )
                if page["status"] == "failed":
                    self._repository.requeue_failed_page(run_id, first)
            source = self._repository.paths_for_run(run_id).source_pdf
            return self._get_run(run_id), source
        except BaseException:
            self._repository.fail_resuming_run(
                run_id,
                "resume_preparation_failed",
                "The run could not be prepared for resume.",
                failure_class="integrity",
            )
            raise

    def _fail_run(self, run_id: str, failure: FailureView) -> RunView:
        row = self._repository.get_run(run_id)
        if row["status"] == "completed":
            return self._run_view(row)
        category = _CATEGORY_MAP.get(failure.category, "internal")
        code = "cancelled" if category == "cancelled" else failure.code
        self._repository.mark_run_failed(
            run_id,
            code,
            _SAFE_MESSAGES[category],
            failure_class=category,
        )
        return self._get_run(run_id)

    def _get_manifest(self, run_id: str) -> Mapping[str, Any]:
        row = self._repository.get_run(run_id)
        if row["status"] != "completed" or not row["final_manifest_path"]:
            raise InvalidRunOperationError(
                "The completed manifest is not available.", run_id=run_id
            )
        self._repository.audit_run(run_id).require_ok()
        path = self._repository.paths_for_run(run_id).root / cast(str, row["final_manifest_path"])
        manifest = ProcessingManifest.model_validate_json(path.read_bytes())
        return cast(Mapping[str, Any], manifest.model_dump(mode="json"))

    def _recover_startup(self) -> tuple[RecoveryItem, ...]:
        self._repository.initialize()
        return recover_durable_runs(
            self._repository,
            configuration_fingerprint=self._fingerprint,
            max_model_attempts=self._max_model_attempts,
        )

    def _run_view(self, row: Mapping[str, Any]) -> RunView:
        pages = self._repository.get_pages(cast(str, row["run_id"]))
        current_page = next(
            (cast(int, page["page_number"]) for page in pages if page["status"] == "running"),
            None,
        )
        total = cast(int | None, row["total_pages"])
        status = RunStatus(cast(str, row["status"]))
        failure = self._failure_view(row)
        return RunView(
            run_id=cast(str, row["run_id"]),
            document_id=cast(str | None, row["source_sha256"]) or "",
            status=status,
            phase=cast(str, row["phase"]),
            progress=ProgressView(
                completed_pages=cast(int, row["head_page"]),
                total_pages=total,
                rendered_pages=cast(int, total) if row["render_manifest_sha256"] else 0,
                current_page=current_page,
            ),
            resumable=(
                status is RunStatus.FAILED
                and row["failure_class"] != "integrity"
                and row["source_sha256"] is not None
                and row["config_sha256"] == self._fingerprint
                and self._has_attempt_budget(cast(str, row["run_id"]))
            ),
            cancel_requested=bool(row["cancel_requested"]),
            created_at=datetime.fromisoformat(cast(str, row["created_at"])),
            updated_at=datetime.fromisoformat(cast(str, row["updated_at"])),
            failure=failure,
        )

    def _page_view(self, row: Mapping[str, Any]) -> PageView:
        return PageView(
            page_number=cast(int, row["page_number"]),
            status=PageStatus(cast(str, row["status"])),
            page_image_path=cast(str | None, row["image_path"]),
            page_json_path=cast(str | None, row["page_json_path"]),
            attempt_count=cast(int, row["attempt_count"]),
            failure=self._failure_view(row),
        )

    def _has_attempt_budget(self, run_id: str) -> bool:
        pages = self._repository.get_pages(run_id)
        first = next((page for page in pages if page["status"] != "completed"), None)
        return first is None or (
            first["status"] != "skipped"
            and cast(int, first["attempt_count"]) < self._max_model_attempts
        )

    @staticmethod
    def _failure_view(row: Mapping[str, Any]) -> FailureView | None:
        code = cast(str | None, row.get("failure_code") or row.get("error_code"))
        if code is None:
            return None
        raw_category = cast(str | None, row.get("failure_class") or row.get("error_class"))
        category = _CATEGORY_MAP.get(raw_category or "", "internal")
        return FailureView(
            code="cancelled" if category == "cancelled" else code,
            category=category,
            message=_SAFE_MESSAGES[category],
            retryable=category == "transient_model",
        )

    async def _translate[T](
        self,
        run_id: str,
        function: Any,
        *args: Any,
    ) -> T:
        try:
            return cast(T, await self._offloads.run(function, *args))
        except RunNotFound as error:
            raise RunNotFoundError("The requested run does not exist.", run_id=run_id) from error
        except (InvalidTransition, IntegrityError) as error:
            raise InvalidRunOperationError(
                "The requested lifecycle operation is not allowed.", run_id=run_id
            ) from error
