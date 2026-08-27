"""Integrity auditing and conservative crash recovery."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .attempt_audit import validate_attempt_state
from .commit_audit import (
    completed_attempt_index,
    validate_attempt_link,
    validate_commit_metadata,
)
from .commits import verify_commit
from .contract_validation import validate_initial_json, validate_page_json
from .files import configuration_fingerprint, resolve_artifact, sha256_file
from .final_audit import check_completed_output
from .lifecycle import utc_now
from .models import AuditIssue, AuditReport, RecoveryReport
from .recovery_support import RecoverySupportMixin
from .render_metadata import validate_render_manifest


class AuditMixin(RecoverySupportMixin):
    def audit_run(self, run_id: str) -> AuditReport:
        run = self.get_run(run_id)
        paths = self.paths_for_run(run_id)
        issues: list[AuditIssue] = []
        self._check_database(issues)
        self._check_run_manifest(run, paths.run_manifest, issues)
        pages = self.get_pages(run_id)
        self._check_source_and_renders(run, pages, paths.root, issues)
        commits = self._commit_rows(run_id)
        referenced = {row["relative_path"] for row in commits}
        self._check_page_rows(run, pages, commits, issues)
        try:
            validate_attempt_state(self._db, run, pages)
        except Exception as error:
            issues.append(AuditIssue("attempt_state_invalid", str(error)))
        self._check_commits(run, pages, commits, paths.root, issues)
        check_completed_output(self, run, pages, paths.root, issues)
        orphans = self._find_orphans(paths, referenced, run)
        first_incomplete = next(
            (row["page_number"] for row in pages if row["status"] != "completed"), None
        )
        return AuditReport(
            run_id=run_id,
            ok=not issues and not orphans,
            head_page=run["head_page"],
            first_incomplete_page=first_incomplete,
            issues=tuple(issues),
            orphan_paths=tuple(orphans),
        )

    def recover_run(self, run_id: str) -> RecoveryReport:
        completed = False
        with self._db.immediate() as connection:
            run = self._require_run(connection, run_id)
            if run["status"] == "completed":
                completed = True
            elif run["status"] == "resuming":
                connection.execute(
                    """UPDATE runs SET phase='recovering',updated_at=?
                       WHERE run_id=?""",
                    (utc_now(), run_id),
                )
        if completed:
            initial = self.audit_run(run_id)
            quarantined = self._quarantine_orphans(run_id, initial.orphan_paths, record_event=False)
            return RecoveryReport(audit=self.audit_run(run_id), quarantined=quarantined)
        render_quarantine = self._quarantine_unreferenced_render_bundle(run_id)
        source_quarantine = self._quarantine_unreferenced_source(run_id)
        initial_audit = self.audit_run(run_id)
        quarantined = self._quarantine_orphans(run_id, initial_audit.orphan_paths)
        if source_quarantine:
            quarantined.insert(0, source_quarantine)
        if render_quarantine:
            quarantined.insert(0, render_quarantine)
        reset_page = self._reset_interrupted_attempt(run_id)
        audit = self.audit_run(run_id)
        if not audit.ok:
            detail = "; ".join(f"{issue.code}: {issue.message}" for issue in audit.issues)
            self.mark_run_integrity_failed(run_id, "integrity_audit_failed", detail)
            self._record_recovery_event(
                run_id, "integrity_failure", {"issues": [issue.__dict__ for issue in audit.issues]}
            )
            return RecoveryReport(audit=audit, quarantined=quarantined, reset_page=reset_page)
        status = self._finish_recovery_state(run_id, audit.first_incomplete_page)
        if status == "completed":
            audit = self.audit_run(run_id)
        return RecoveryReport(audit=audit, quarantined=quarantined, reset_page=reset_page)

    def load_checkpoint(self, run_id: str) -> Mapping[str, Any]:
        audit = self.audit_run(run_id).require_ok()
        run = self.get_run(run_id)
        commit = self._get_commit_row(run_id, audit.head_page)
        root = self.paths_for_run(run_id).root
        directory = resolve_artifact(root, commit["relative_path"])
        return {
            "run_id": run_id,
            "head_page": audit.head_page,
            "head_commit_id": run["head_commit_id"],
            "first_incomplete_page": audit.first_incomplete_page,
            "short_term_memory": json.loads(
                (directory / "short_term_memory.json").read_text(encoding="utf-8")
            ),
            "document_structure": json.loads(
                (directory / "document_structure.json").read_text(encoding="utf-8")
            ),
        }

    def _check_database(self, issues: list[AuditIssue]) -> None:
        try:
            with self._db.connect() as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                issues.append(AuditIssue("database_integrity", str(result), "state.sqlite3"))
        except Exception as error:
            issues.append(AuditIssue("database_unreadable", str(error), "state.sqlite3"))

    @staticmethod
    def _issue(issues: list[AuditIssue], code: str, message: str, path: Path) -> None:
        issues.append(AuditIssue(code, message, str(path)))

    @staticmethod
    def _check_run_manifest(run: Mapping[str, Any], path: Path, issues: list[AuditIssue]) -> None:
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("run manifest is missing or unsafe")
            payload = json.loads(path.read_text(encoding="utf-8"))
            expected = {
                "schema_version": 1,
                "run_id": run["run_id"],
                "run_statuses": ["not_started", "running", "completed", "failed", "resuming"],
                "page_statuses": ["pending", "running", "completed", "failed", "skipped"],
                "config": json.loads(run["config_json"]),
                "config_fingerprint": run["config_sha256"],
            }
            expected_fingerprint = configuration_fingerprint(expected["config"])
            if expected_fingerprint != run["config_sha256"]:
                raise ValueError("stored configuration fingerprint is invalid")
            if payload != expected:
                raise ValueError("run manifest differs from the durable run record")
        except Exception as error:
            issues.append(AuditIssue("run_manifest_invalid", str(error), "run.json"))

    def _check_source_and_renders(
        self,
        run: Mapping[str, Any],
        pages: Sequence[Mapping[str, Any]],
        root: Path,
        issues: list[AuditIssue],
    ) -> None:
        source_rel = run.get("source_path")
        if not source_rel or not run.get("source_sha256"):
            if (
                source_rel
                or run.get("source_sha256")
                or run.get("render_manifest_path")
                or run.get("render_manifest_sha256")
                or run.get("head_commit_id") is not None
                or run.get("total_pages") is not None
                or pages
            ):
                issues.append(
                    AuditIssue("source_metadata_missing", "source metadata is incomplete")
                )
            return
        try:
            source = resolve_artifact(root, source_rel)
            if not source.is_file() or sha256_file(source) != run["source_sha256"]:
                self._issue(issues, "source_invalid", "source PDF is missing or changed", source)
            source_entries = list(source.parent.iterdir())
            if {path.name for path in source_entries} != {"original.pdf"} or any(
                path.is_symlink() or not path.is_file() for path in source_entries
            ):
                raise ValueError("source directory contains unexpected or unsafe entries")
        except Exception as error:
            issues.append(AuditIssue("source_invalid", str(error), source_rel))
        manifest_rel = run.get("render_manifest_path")
        if not manifest_rel or not run.get("render_manifest_sha256"):
            if (
                manifest_rel
                or run.get("render_manifest_sha256")
                or run.get("head_commit_id") is not None
                or run.get("total_pages") is not None
            ):
                issues.append(
                    AuditIssue("render_metadata_missing", "render metadata is incomplete")
                )
            return
        try:
            manifest_path = resolve_artifact(root, manifest_rel)
            if manifest_path.is_symlink() or not manifest_path.is_file():
                raise ValueError("render manifest is missing or unsafe")
            if sha256_file(manifest_path) != run["render_manifest_sha256"]:
                raise ValueError("manifest hash changed")
            manifest, expected_rows = validate_render_manifest(
                self.paths_for_run(run["run_id"]), manifest_path.read_bytes(), run
            )
            if manifest.page_count != run["total_pages"]:
                raise ValueError("manifest page count mismatch")
            if len(expected_rows) != len(pages):
                raise ValueError("render manifest pages mismatch")
            for expected, stored in zip(expected_rows, pages, strict=True):
                actual = (
                    stored["page_number"],
                    stored["image_path"],
                    stored["image_sha256"],
                    stored["image_width"],
                    stored["image_height"],
                )
                if actual != expected:
                    raise ValueError("render manifest disagrees with the page ledger")
        except Exception as error:
            issues.append(AuditIssue("render_manifest_invalid", str(error), manifest_rel))

    @staticmethod
    def _check_page_rows(
        run: Mapping[str, Any],
        pages: Sequence[Mapping[str, Any]],
        commits: Sequence[Mapping[str, Any]],
        issues: list[AuditIssue],
    ) -> None:
        total = run.get("total_pages")
        if total is None and not pages and run.get("head_commit_id") is None:
            return
        if total is None or len(pages) != total:
            issues.append(AuditIssue("page_count_mismatch", "page rows do not match PDF count"))
            return
        if [row["page_number"] for row in pages] != list(range(1, total + 1)):
            issues.append(AuditIssue("page_order_invalid", "page rows are not contiguous"))
        completed = [row["page_number"] for row in pages if row["status"] == "completed"]
        if completed != list(range(1, run["head_page"] + 1)):
            issues.append(
                AuditIssue("completed_prefix_invalid", "completed pages disagree with head")
            )
        expected_commits = run["head_page"] + 1 if run["head_commit_id"] else 0
        if len(commits) != expected_commits:
            issues.append(AuditIssue("commit_count_invalid", "commit count disagrees with head"))
        if run["status"] == "completed" and run["head_page"] != total:
            issues.append(AuditIssue("completed_run_incomplete", "completed run lacks all pages"))

    def _check_commits(
        self,
        run: Mapping[str, Any],
        pages: Sequence[Mapping[str, Any]],
        commits: Sequence[Mapping[str, Any]],
        root: Path,
        issues: list[AuditIssue],
    ) -> None:
        previous: str | None = None
        previous_snapshots: tuple[bytes, bytes] | None = None
        page_by_number = {row["page_number"]: row for row in pages}
        try:
            attempts = completed_attempt_index(self._db, run["run_id"], commits)
        except Exception as error:
            issues.append(AuditIssue("attempt_ledger_invalid", str(error)))
            attempts = {}
        for expected_page, row in enumerate(commits):
            relative = row["relative_path"]
            expected_relative = (
                "initial" if expected_page == 0 else f"page_artifacts/page-{expected_page:04d}"
            )
            if row["page_number"] != expected_page or relative != expected_relative:
                issues.append(
                    AuditIssue("commit_order_invalid", "unexpected commit path/order", relative)
                )
                continue
            try:
                directory = resolve_artifact(root, relative)
                manifest = verify_commit(directory, row["commit_id"])
                validate_commit_metadata(run, row, manifest, previous)
                memory = (directory / "short_term_memory.json").read_bytes()
                structure = (directory / "document_structure.json").read_bytes()
                if expected_page == 0:
                    validate_initial_json(memory, structure)
                else:
                    if previous_snapshots is None:
                        raise ValueError("prior commit snapshot did not pass integrity validation")
                    page = page_by_number[expected_page]
                    if page["commit_id"] != row["commit_id"]:
                        raise ValueError("page row references a different commit")
                    expected_page_path = f"{relative}/page.json"
                    if page["page_json_path"] != expected_page_path:
                        raise ValueError("page row has an invalid page JSON path")
                    if page["page_json_sha256"] != manifest["files"]["page.json"]:
                        raise ValueError("page row has an invalid page JSON hash")
                    if (
                        manifest["image_path"] != page["image_path"]
                        or manifest["image_sha256"] != page["image_sha256"]
                    ):
                        raise ValueError("commit image binding differs from the page ledger")
                    if (
                        sha256_file(resolve_artifact(root, page["image_path"]))
                        != page["image_sha256"]
                    ):
                        raise ValueError("page image is missing or changed")
                    usage = (directory / "usage.json").read_bytes()
                    validate_page_json(
                        page_number=expected_page,
                        image_path=page["image_path"],
                        page=(directory / "page.json").read_bytes(),
                        memory=memory,
                        structure=structure,
                        model_response=(directory / "model_response.json").read_bytes(),
                        usage=usage,
                        prior_memory=previous_snapshots[0],
                        prior_structure=previous_snapshots[1],
                    )
                    validate_attempt_link(
                        attempts, row, page_number=expected_page, usage_bytes=usage
                    )
                previous = row["commit_id"]
                previous_snapshots = (memory, structure)
            except Exception as error:
                issues.append(AuditIssue("commit_invalid", str(error), relative))
        if commits and run["head_commit_id"] != commits[-1]["commit_id"]:
            issues.append(AuditIssue("head_mismatch", "run head is not the last commit"))
