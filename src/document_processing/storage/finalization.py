"""Preparation and transactional publication of the final output bundle."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from document_processing.contracts import ProcessingManifest

from .audit import AuditMixin
from .files import (
    canonical_json,
    fsync_directory,
    jsonable,
    quarantine,
    resolve_artifact,
    sha256_bytes,
    write_durable,
)
from .final_audit import check_completed_output
from .lifecycle import utc_now
from .models import (
    AuditIssue,
    CancelCheck,
    CancelledBeforeCommit,
    FaultHook,
    FinalManifest,
    IntegrityError,
    InvalidTransition,
)


class FinalizationMixin(AuditMixin):
    def prepare_final_manifest(
        self, run_id: str, manifest: Any, *, fault_hook: FaultHook | None = None
    ) -> FinalManifest:
        report = self.audit_run(run_id)
        if report.orphan_paths:
            self._quarantine_orphans(run_id, report.orphan_paths)
            report = self.audit_run(run_id)
        report.require_ok()
        run = self.get_run(run_id)
        pages = self.get_pages(run_id)
        if report.head_page != run["total_pages"] or any(
            page["status"] != "completed" for page in pages
        ):
            raise InvalidTransition("finalization requires every page to be completed")
        payload = jsonable(manifest)
        if not isinstance(payload, Mapping):
            raise ValueError("final manifest must be an object")
        validated = ProcessingManifest.model_validate(payload)
        self._validate_final_manifest(run, pages, validated)
        payload = validated.model_dump(mode="json", by_alias=True)
        paths = self.paths_for_run(run_id)
        if paths.final_dir.exists():
            if run["status"] == "completed" and run["final_manifest_path"]:
                return FinalManifest(
                    run_id, run["final_manifest_path"], run["final_manifest_sha256"], payload
                )
            raise InvalidTransition("unreferenced final directory requires recovery")
        head = self._get_commit_row(run_id, run["head_page"])
        head_dir = resolve_artifact(paths.root, head["relative_path"])
        staging = paths.staging_dir / f"final-{uuid.uuid4().hex}"
        staging.mkdir(parents=False, exist_ok=False)
        output_bytes = canonical_json(payload)
        usage = self._usage_records(run_id)
        events = self._recovery_records(run_id)
        status = {
            "run_id": run_id,
            "status": "completed",
            "page_count": run["total_pages"],
            "last_committed_page": run["head_page"],
            "head_commit_id": run["head_commit_id"],
            "integrity_audit": "passed",
        }
        try:
            write_durable(staging / "output_manifest.json", output_bytes)
            self._copy_durable(head_dir / "short_term_memory.json", staging)
            self._copy_durable(head_dir / "document_structure.json", staging)
            write_durable(staging / "model_usage.json", canonical_json(usage))
            write_durable(staging / "processing_status.json", canonical_json(status))
            write_durable(staging / "recovery_events.json", canonical_json(events))
            fsync_directory(staging)
            if fault_hook:
                fault_hook("after_final_staging_fsync")
            os.rename(str(staging), str(paths.final_dir))
            fsync_directory(paths.root)
            if fault_hook:
                fault_hook("after_final_rename")
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return FinalManifest(
            run_id=run_id,
            relative_path="final/output_manifest.json",
            sha256=sha256_bytes(output_bytes),
            payload=payload,
        )

    def finalize_run(
        self,
        final: FinalManifest,
        *,
        cancel_check: CancelCheck | None = None,
        fault_hook: FaultHook | None = None,
    ) -> Mapping[str, Any]:
        paths = self.paths_for_run(final.run_id)
        output = resolve_artifact(paths.root, final.relative_path)
        if not output.is_file() or sha256_bytes(output.read_bytes()) != final.sha256:
            raise InvalidTransition("prepared final manifest is missing or changed")
        cancelled = False
        with self._db.immediate() as connection:
            run = self._require_run(connection, final.run_id)
            if run["status"] == "completed":
                if run["final_manifest_sha256"] != final.sha256:
                    raise InvalidTransition("completed run references another final manifest")
                return dict(run)
            if run["status"] != "running":
                raise InvalidTransition("final publication requires a running run")
            should_cancel = bool(run["cancel_requested"]) or (
                cancel_check is not None and cancel_check()
            )
            if not should_cancel:
                incomplete = connection.execute(
                    "SELECT COUNT(*) FROM pages WHERE run_id=? AND status!='completed'",
                    (final.run_id,),
                ).fetchone()[0]
                if incomplete or run["head_page"] != run["total_pages"]:
                    raise InvalidTransition("run changed before final publication")
                self._validate_final_candidate(connection, dict(run), final, paths.root)
                should_cancel = cancel_check is not None and cancel_check()
            if should_cancel:
                connection.execute(
                    """UPDATE runs SET status='failed',phase='failed',
                       failure_class='cancelled',failure_code='cancelled',
                       failure_detail='cancelled_before_finalization',updated_at=?
                       WHERE run_id=?""",
                    (utc_now(), final.run_id),
                )
                cancelled = True
            else:
                connection.execute(
                    """UPDATE runs SET status='completed',phase='completed',
                       final_manifest_path=?,final_manifest_sha256=?,failure_class=NULL,
                       failure_code=NULL,failure_detail=NULL,completed_at=?,updated_at=?
                       WHERE run_id=?""",
                    (
                        final.relative_path,
                        final.sha256,
                        utc_now(),
                        utc_now(),
                        final.run_id,
                    ),
                )
                if fault_hook:
                    fault_hook("before_final_db_commit")
        if cancelled:
            quarantine(paths.final_dir, paths.quarantine_dir, "cancelled-final")
            raise CancelledBeforeCommit(final.run_id)
        if fault_hook:
            fault_hook("after_final_db_commit")
        return self.get_run(final.run_id)

    def mark_completed_integrity_failed(
        self,
        run_id: str,
        detail: str = "The completed run failed its integrity audit.",
    ) -> Mapping[str, Any]:
        """Demote a completed run only when its full durable audit currently fails."""

        initial = self.audit_run(run_id)
        if initial.orphan_paths:
            self._quarantine_orphans(run_id, initial.orphan_paths, record_event=False)
        with self._db.immediate() as connection:
            run = self._require_run(connection, run_id)
            if run["status"] != "completed":
                raise InvalidTransition("integrity demotion requires a completed run")
            report = self.audit_run(run_id)
            if report.ok:
                return dict(run)
            connection.execute(
                """UPDATE runs SET status='failed',phase='failed',failure_class='integrity',
                   failure_code='integrity_audit_failed',failure_detail=?,updated_at=?
                   WHERE run_id=?""",
                (detail, utc_now(), run_id),
            )
        return self.get_run(run_id)

    def commit_final_manifest(
        self,
        run_id: str,
        manifest: Any,
        *,
        cancel_check: CancelCheck | None = None,
        fault_hook: FaultHook | None = None,
    ) -> Mapping[str, Any]:
        prepared = self.prepare_final_manifest(run_id, manifest, fault_hook=fault_hook)
        return self.finalize_run(prepared, cancel_check=cancel_check, fault_hook=fault_hook)

    def _validate_final_candidate(
        self,
        connection: sqlite3.Connection,
        run: Mapping[str, Any],
        final: FinalManifest,
        root: Path,
    ) -> None:
        base_audit = self.audit_run(final.run_id)
        unexpected_orphans = [path for path in base_audit.orphan_paths if path != "final"]
        if base_audit.issues or unexpected_orphans:
            parts = [f"{issue.code}: {issue.message}" for issue in base_audit.issues]
            parts.extend(f"unreferenced artifact: {path}" for path in unexpected_orphans)
            detail = "; ".join(parts)
            raise IntegrityError(detail)
        candidate = {
            **dict(run),
            "status": "completed",
            "phase": "completed",
            "final_manifest_path": final.relative_path,
            "final_manifest_sha256": final.sha256,
        }
        pages = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM pages WHERE run_id=? ORDER BY page_number",
                (final.run_id,),
            )
        ]
        issues: list[AuditIssue] = []
        check_completed_output(self, candidate, pages, root, issues)
        if issues:
            detail = "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
            raise IntegrityError(detail)

    @staticmethod
    def _validate_final_manifest(
        run: Mapping[str, Any],
        pages: Sequence[Mapping[str, Any]],
        manifest: ProcessingManifest,
    ) -> None:
        if manifest.run_id != run["run_id"] or manifest.status.value != "completed":
            raise ValueError("final manifest must identify a completed current run")
        if manifest.schema_version != "1":
            raise ValueError("unsupported final manifest schema version")
        if manifest.document_id != run["source_sha256"]:
            raise ValueError("final manifest document id mismatch")
        if manifest.source_pdf_path != run["source_path"]:
            raise ValueError("final manifest source path mismatch")
        if manifest.source_sha256 != run["source_sha256"]:
            raise ValueError("final manifest source hash mismatch")
        if manifest.config_fingerprint != run["config_sha256"]:
            raise ValueError("final manifest configuration mismatch")
        if manifest.page_count != run["total_pages"]:
            raise ValueError("final manifest page count mismatch")
        if manifest.last_committed_page != run["head_page"]:
            raise ValueError("final manifest head mismatch")
        for expected, actual in zip(pages, manifest.pages, strict=True):
            if actual.page_image_path != expected["image_path"]:
                raise ValueError("final manifest page image mismatch")
            if actual.page_json_path != expected["page_json_path"]:
                raise ValueError("final manifest page JSON mismatch")
        if manifest.short_term_memory_path != "final/short_term_memory.json":
            raise ValueError("final manifest must reference the final memory artifact")
        if manifest.document_structure_path != "final/document_structure.json":
            raise ValueError("final manifest must reference the final structure artifact")

    @staticmethod
    def _copy_durable(source: Path, destination_dir: Path) -> None:
        if source.is_symlink() or not source.is_file():
            raise IntegrityError(f"final source artifact is missing or unsafe: {source}")
        write_durable(destination_dir / source.name, source.read_bytes())

    def _usage_records(self, run_id: str) -> list[Any]:
        with self._db.connect() as connection:
            rows = connection.execute(
                """SELECT usage_json FROM attempts WHERE run_id=? AND status='completed'
                   ORDER BY page_number,ordinal""",
                (run_id,),
            )
            return [json.loads(row["usage_json"]) for row in rows]

    def _recovery_records(self, run_id: str) -> list[Mapping[str, Any]]:
        with self._db.connect() as connection:
            rows = connection.execute(
                """SELECT event_type,details_json,created_at FROM recovery_events
                   WHERE run_id=? ORDER BY event_id""",
                (run_id,),
            )
            return [
                {
                    "event_type": row["event_type"],
                    "details": json.loads(row["details_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
