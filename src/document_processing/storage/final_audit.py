"""Completed-output validation kept separate from in-progress commit auditing."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from document_processing.contracts import ProcessingManifest

from .files import resolve_artifact, sha256_file
from .models import FINAL_FILES, AuditIssue


def check_completed_output(
    repository: Any,
    run: Mapping[str, Any],
    pages: Sequence[Mapping[str, Any]],
    root: Path,
    issues: list[AuditIssue],
) -> None:
    if run["status"] != "completed":
        return
    relative = run.get("final_manifest_path")
    digest = run.get("final_manifest_sha256")
    if relative != "final/output_manifest.json" or not digest:
        issues.append(AuditIssue("final_metadata_missing", "completed run lacks final metadata"))
        return
    final_dir = root / "final"
    try:
        entries = list(final_dir.iterdir())
        names = {path.name for path in entries}
        if names != set(FINAL_FILES) or any(
            path.is_symlink() or not path.is_file() for path in entries
        ):
            raise ValueError("final directory does not contain exactly the approved files")
        output_path = resolve_artifact(root, relative)
        if sha256_file(output_path) != digest:
            raise ValueError("final manifest hash changed")
        manifest = ProcessingManifest.model_validate_json(output_path.read_bytes())
        _validate_manifest(run, pages, manifest)
        head = repository._get_commit_row(run["run_id"], run["head_page"])
        head_dir = resolve_artifact(root, head["relative_path"])
        _require_equal(
            final_dir / "short_term_memory.json",
            head_dir / "short_term_memory.json",
            "final short-term memory differs from the committed head",
        )
        _require_equal(
            final_dir / "document_structure.json",
            head_dir / "document_structure.json",
            "final document structure differs from the committed head",
        )
        usage = json.loads((final_dir / "model_usage.json").read_text(encoding="utf-8"))
        if usage != repository._usage_records(run["run_id"]):
            raise ValueError("final model usage differs from the attempt ledger")
        events = json.loads((final_dir / "recovery_events.json").read_text(encoding="utf-8"))
        if events != repository._recovery_records(run["run_id"]):
            raise ValueError("final recovery events differ from the recovery ledger")
        status = json.loads((final_dir / "processing_status.json").read_text(encoding="utf-8"))
        expected_status = {
            "run_id": run["run_id"],
            "status": "completed",
            "page_count": run["total_pages"],
            "last_committed_page": run["head_page"],
            "head_commit_id": run["head_commit_id"],
            "integrity_audit": "passed",
        }
        if status != expected_status:
            raise ValueError("final processing status differs from durable run state")
    except Exception as error:
        issues.append(AuditIssue("final_output_invalid", str(error), "final"))


def _validate_manifest(
    run: Mapping[str, Any],
    pages: Sequence[Mapping[str, Any]],
    manifest: ProcessingManifest,
) -> None:
    if manifest.schema_version != "1":
        raise ValueError("unsupported final manifest schema version")
    if manifest.run_id != run["run_id"] or manifest.document_id != run["source_sha256"]:
        raise ValueError("final manifest identity mismatch")
    if manifest.status.value != "completed" or manifest.error is not None:
        raise ValueError("final manifest is not a successful completion")
    expected = (
        run["source_path"],
        run["source_sha256"],
        run["config_sha256"],
        run["total_pages"],
        run["head_page"],
    )
    actual = (
        manifest.source_pdf_path,
        manifest.source_sha256,
        manifest.config_fingerprint,
        manifest.page_count,
        manifest.last_committed_page,
    )
    if actual != expected:
        raise ValueError("final manifest run metadata mismatch")
    if manifest.short_term_memory_path != "final/short_term_memory.json":
        raise ValueError("final manifest memory path mismatch")
    if manifest.document_structure_path != "final/document_structure.json":
        raise ValueError("final manifest structure path mismatch")
    for stored, exported in zip(pages, manifest.pages, strict=True):
        if exported.page_number != stored["page_number"]:
            raise ValueError("final manifest page order mismatch")
        if exported.status.value != stored["status"]:
            raise ValueError("final manifest page status mismatch")
        if exported.page_image_path != stored["image_path"]:
            raise ValueError("final manifest page image mismatch")
        if exported.page_json_path != stored["page_json_path"]:
            raise ValueError("final manifest page JSON mismatch")
        if exported.attempt_count != stored["attempt_count"] or exported.error is not None:
            raise ValueError("final manifest page state mismatch")


def _require_equal(left: Path, right: Path, message: str) -> None:
    if left.is_symlink() or not left.is_file() or right.is_symlink() or not right.is_file():
        raise ValueError("final comparison includes a missing or unsafe artifact")
    if left.read_bytes() != right.read_bytes():
        raise ValueError(message)
