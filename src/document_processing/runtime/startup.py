"""Conservative startup reconciliation for durable and unjournaled runs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from document_processing.service.models import RecoveryItem
from document_processing.storage import IntegrityError, RunRepository
from document_processing.storage.files import quarantine


def recover_durable_runs(
    repository: RunRepository,
    *,
    configuration_fingerprint: str,
    max_model_attempts: int,
) -> tuple[RecoveryItem, ...]:
    """Audit every run and return interrupted current-config work in FIFO order."""

    rows = repository.list_runs()
    _quarantine_unregistered(repository, rows)
    recoveries: list[RecoveryItem] = []
    for row in rows:
        run_id = cast(str, row["run_id"])
        if row["status"] == "completed":
            _audit_completed(repository, run_id)
            continue
        if row["status"] == "failed":
            _audit_failed(repository, row)
            continue
        if row["config_sha256"] != configuration_fingerprint:
            repository.mark_run_failed(
                run_id,
                "configuration_mismatch",
                "The run configuration differs from the current service.",
                failure_class="configuration",
            )
            continue
        recovery = repository.recover_run(run_id)
        if not recovery.audit.ok:
            continue
        current = repository.get_run(run_id)
        if current["status"] == "failed":
            continue
        if not _has_attempt_budget(repository, run_id, max_model_attempts):
            repository.mark_run_failed(
                run_id,
                "model_transient_exhausted",
                "The page exhausted its durable model-attempt budget.",
                failure_class="transient_model",
            )
            continue
        claimed = repository.claim_recovered_run(run_id)
        if claimed["status"] != "resuming":
            continue
        recoveries.append(
            RecoveryItem(
                run_id=run_id,
                source_path=repository.paths_for_run(run_id).source_pdf,
                resume=True,
            )
        )
    return tuple(recoveries)


def _audit_completed(repository: RunRepository, run_id: str) -> None:
    if repository.audit_run(run_id).ok:
        return
    repository.mark_completed_integrity_failed(
        run_id,
        "The completed run failed its startup integrity audit.",
    )


def _audit_failed(repository: RunRepository, row: Mapping[str, Any]) -> None:
    run_id = cast(str, row["run_id"])
    recovery = repository.recover_run(run_id)
    if not recovery.audit.ok:
        return
    repository.mark_run_failed(
        run_id,
        cast(str | None, row["failure_code"]) or "processing_failed",
        "The failed run remains available for an explicit audited resume.",
        failure_class=cast(str | None, row["failure_class"]) or "internal",
    )


def _has_attempt_budget(
    repository: RunRepository,
    run_id: str,
    max_model_attempts: int,
) -> bool:
    first = next(
        (page for page in repository.get_pages(run_id) if page["status"] != "completed"),
        None,
    )
    return first is None or (
        first["status"] != "skipped" and cast(int, first["attempt_count"]) < max_model_attempts
    )


def _quarantine_unregistered(
    repository: RunRepository,
    rows: list[Mapping[str, Any]],
) -> None:
    runs_directory = repository.root / "runs"
    quarantine_directory = repository.root / "quarantine"
    if runs_directory.is_symlink() or not runs_directory.is_dir():
        raise IntegrityError("the durable runs directory is unsafe")
    quarantine_directory.mkdir(exist_ok=True)
    if quarantine_directory.is_symlink() or not quarantine_directory.is_dir():
        raise IntegrityError("the root quarantine directory is unsafe")
    known = {cast(str, row["run_id"]) for row in rows}
    for path in sorted(runs_directory.iterdir(), key=lambda item: item.name):
        if path.name not in known:
            quarantine(path, quarantine_directory, "unregistered-run")
