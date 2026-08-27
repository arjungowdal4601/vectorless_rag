"""Final completion audit and durable manifest publication."""

from __future__ import annotations

from document_processing.contracts import ProcessingManifest
from document_processing.processing.cancellation import ProcessingCancelled
from document_processing.processing.errors import (
    FailureCode,
    commit_was_cancelled,
    sanitized_failure,
    stage_failure,
)
from document_processing.processing.interfaces import (
    PreparedPdf,
    ProcessingRepository,
    RunHandle,
)
from document_processing.processing.lifecycle import CancellationScope, failed_result
from document_processing.processing.models import ProcessingResult


async def finalize_run(
    repository: ProcessingRepository,
    run: RunHandle,
    prepared: PreparedPdf,
    completed: int,
    scope: CancellationScope,
) -> ProcessingResult:
    """Publish a manifest only after a complete repository audit."""

    total = prepared.manifest.page_count
    try:
        await scope.check()
        audit = await scope.offloads.run(
            repository.audit_completion,
            run.run_id,
            prepared=prepared,
        )
        if not audit.complete or audit.manifest is None:
            detail = "; ".join(audit.issues) or "completion audit was incomplete"
            raise ValueError(detail)
        await scope.check()
        manifest = await scope.offloads.run(
            repository.finalize_completion,
            run.run_id,
            audit=audit,
            cancel_check=scope.cancelled,
        )
    except ProcessingCancelled as error:
        failure = sanitized_failure(error)
    except Exception as error:
        if commit_was_cancelled(error):
            failure = sanitized_failure(ProcessingCancelled("cancelled at final commit boundary"))
        else:
            try:
                authoritative = await scope.offloads.run(
                    repository.audit_completion,
                    run.run_id,
                    prepared=prepared,
                )
            except Exception:
                authoritative = None
            if (
                authoritative is not None
                and authoritative.complete
                and authoritative.published
                and authoritative.manifest is not None
            ):
                manifest = authoritative.manifest
                return _completed_result(run, prepared, completed, total, manifest)
            failure = stage_failure(
                FailureCode.INTEGRITY_FAILED,
                "The run failed its final completion audit or manifest commit.",
                error,
            )
    else:
        return _completed_result(run, prepared, completed, total, manifest)
    return await failed_result(
        repository,
        run,
        document_id=prepared.document_id,
        completed=completed,
        total=total,
        failure=failure,
        offloads=scope.offloads,
    )


def _completed_result(
    run: RunHandle,
    prepared: PreparedPdf,
    completed: int,
    total: int,
    manifest: ProcessingManifest,
) -> ProcessingResult:
    return ProcessingResult(
        run_id=run.run_id,
        document_id=prepared.document_id,
        status="completed",
        completed_pages=completed,
        total_pages=total,
        artifact_root=run.run_directory,
        manifest_path=run.run_directory / "final/output_manifest.json",
        manifest=manifest,
    )
