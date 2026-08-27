"""Filesystem reconciliation and recovery-ledger helpers."""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from .files import (
    canonical_json,
    fsync_directory,
    quarantine,
    relative_to_run,
    validate_relative_path,
)
from .lifecycle import utc_now
from .models import RunPaths
from .page_operations import PageOperationsMixin


class RecoverySupportMixin(PageOperationsMixin):
    def _finish_recovery_state(self, run_id: str, first_incomplete: int | None) -> str:
        """Publish recovery state without overwriting a resume/cancel/finalize race."""

        with self._db.immediate() as connection:
            run = self._require_run(connection, run_id)
            if run["status"] in {"completed", "failed"}:
                return cast(str, run["status"])
            now = utc_now()
            if run["cancel_requested"]:
                connection.execute(
                    """UPDATE runs SET status='failed',phase='failed',
                       failure_class='cancelled',failure_code='cancelled',
                       failure_detail='cancel_requested',updated_at=? WHERE run_id=?""",
                    (now, run_id),
                )
                return "failed"
            if run["status"] == "resuming":
                connection.execute(
                    "UPDATE runs SET phase='recovering',updated_at=? WHERE run_id=?",
                    (now, run_id),
                )
                return "resuming"
            if first_incomplete is not None:
                row = connection.execute(
                    "SELECT status FROM pages WHERE run_id=? AND page_number=?",
                    (run_id, first_incomplete),
                ).fetchone()
                if row is not None and row["status"] in {"failed", "skipped"}:
                    connection.execute(
                        """UPDATE runs SET status='failed',phase='failed',updated_at=?
                           WHERE run_id=?""",
                        (now, run_id),
                    )
                    return "failed"
            phase = (
                "processing"
                if run["head_commit_id"] is not None
                else ("rendering" if run["source_sha256"] else "preparing")
            )
            connection.execute(
                """UPDATE runs SET phase=?,failure_class=NULL,failure_code=NULL,
                   failure_detail=NULL,updated_at=? WHERE run_id=?""",
                (phase, now, run_id),
            )
            return cast(str, run["status"])

    def _quarantine_unreferenced_source(self, run_id: str) -> str | None:
        run = self.get_run(run_id)
        if run["source_path"] or run["source_sha256"]:
            return None
        paths = self.paths_for_run(run_id)
        source = paths.source_pdf
        if not source.exists() and not source.is_symlink():
            return None
        destination = quarantine(source, paths.quarantine_dir, "unreferenced-source")
        relative = relative_to_run(destination, paths.root)
        self._record_recovery_event(
            run_id,
            "unreferenced_source_quarantined",
            {"destination": relative},
        )
        return relative

    def _quarantine_unreferenced_render_bundle(self, run_id: str) -> str | None:
        run = self.get_run(run_id)
        paths = self.paths_for_run(run_id)
        if run["render_manifest_path"] or run["render_manifest_sha256"]:
            return None
        manifest_present = paths.render_manifest.exists() or paths.render_manifest.is_symlink()
        images_present = paths.page_images_dir.exists() or paths.page_images_dir.is_symlink()
        images_need_quarantine = images_present and (
            paths.page_images_dir.is_symlink()
            or not paths.page_images_dir.is_dir()
            or any(paths.page_images_dir.iterdir())
        )
        if not manifest_present and not images_need_quarantine:
            return None
        paths.quarantine_dir.mkdir(parents=True, exist_ok=True)
        destination = paths.quarantine_dir / f"unreferenced-render-{uuid.uuid4().hex}"
        destination.mkdir()
        fsync_directory(paths.quarantine_dir)
        if manifest_present:
            os.rename(paths.render_manifest, destination / paths.render_manifest.name)
            fsync_directory(destination)
            fsync_directory(paths.root)
        if images_need_quarantine:
            os.rename(paths.page_images_dir, destination / paths.page_images_dir.name)
            fsync_directory(destination)
            fsync_directory(paths.root)
        paths.page_images_dir.mkdir(exist_ok=True)
        fsync_directory(destination)
        fsync_directory(paths.quarantine_dir)
        fsync_directory(paths.root)
        relative = relative_to_run(destination, paths.root)
        self._record_recovery_event(
            run_id,
            "unreferenced_render_quarantined",
            {"destination": relative},
        )
        return relative

    def _find_orphans(
        self,
        paths: RunPaths,
        referenced: set[str],
        run: Mapping[str, Any],
    ) -> list[str]:
        candidates: list[Path] = list(paths.staging_dir.iterdir())
        candidates.extend(paths.page_artifacts_dir.iterdir())
        if paths.initial_dir.exists():
            candidates.append(paths.initial_dir)
        if (
            paths.final_dir.exists()
            and run.get("final_manifest_path") != "final/output_manifest.json"
        ):
            candidates.append(paths.final_dir)
        source_unbound = not run.get("source_path") and not run.get("source_sha256")
        render_unbound = not run.get("render_manifest_path") and not run.get(
            "render_manifest_sha256"
        )
        if (paths.source_pdf.exists() or paths.source_pdf.is_symlink()) and source_unbound:
            candidates.append(paths.source_pdf)
        if (
            paths.render_manifest.exists() or paths.render_manifest.is_symlink()
        ) and render_unbound:
            candidates.append(paths.render_manifest)
        if render_unbound and any(paths.page_images_dir.iterdir()):
            candidates.append(paths.page_images_dir)
        allowed_root = {
            ".staging",
            "final",
            "initial",
            "page_artifacts",
            "page_images",
            "quarantine",
            "render_manifest.json",
            "run.json",
            "source",
        }
        candidates.extend(path for path in paths.root.iterdir() if path.name not in allowed_root)
        candidates = list(dict.fromkeys(candidates))
        return sorted(
            relative_to_run(path, paths.root)
            for path in candidates
            if relative_to_run(path, paths.root) not in referenced
            and not any(
                ref.startswith(relative_to_run(path, paths.root) + "/") for ref in referenced
            )
        )

    def _quarantine_orphans(
        self, run_id: str, relatives: Sequence[str], *, record_event: bool = True
    ) -> list[str]:
        paths = self.paths_for_run(run_id)
        moved: list[str] = []
        for relative in relatives:
            parsed = validate_relative_path(relative)
            path = paths.root.joinpath(*parsed.parts)
            if not path.exists() and not path.is_symlink():
                continue
            destination = quarantine(path, paths.quarantine_dir, "orphan")
            moved.append(relative_to_run(destination, paths.root))
            if record_event:
                self._record_recovery_event(
                    run_id,
                    "orphan_quarantined",
                    {"source": relative, "destination": moved[-1]},
                )
        return moved

    def _reset_interrupted_attempt(self, run_id: str) -> int | None:
        with self._db.immediate() as connection:
            run = self._require_run(connection, run_id)
            rows = connection.execute(
                """SELECT * FROM pages WHERE run_id=? AND status='running'
                   ORDER BY page_number""",
                (run_id,),
            ).fetchall()
            if not rows:
                return None
            if len(rows) != 1 or rows[0]["page_number"] != run["head_page"] + 1:
                return None
            page = rows[0]
            now = utc_now()
            connection.execute(
                """UPDATE attempts SET status='interrupted',failure_class='storage',
                   failure_code='process_interrupted',finished_at=? WHERE attempt_id=?
                   AND status='running'""",
                (now, page["active_attempt_id"]),
            )
            connection.execute(
                """UPDATE pages SET status='pending',active_attempt_id=NULL WHERE
                   run_id=? AND page_number=?""",
                (run_id, page["page_number"]),
            )
        page_number = cast(int, page["page_number"])
        self._record_recovery_event(
            run_id, "interrupted_attempt_reset", {"page_number": page_number}
        )
        return page_number

    def _record_recovery_event(self, run_id: str, event_type: str, details: Any) -> None:
        with self._db.immediate() as connection:
            connection.execute(
                """INSERT INTO recovery_events(run_id,event_type,details_json,created_at)
                   VALUES(?,?,?,?)""",
                (run_id, event_type, canonical_json(details).decode("utf-8"), utc_now()),
            )

    def _commit_rows(self, run_id: str) -> list[Mapping[str, Any]]:
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM commits WHERE run_id=? ORDER BY page_number", (run_id,)
            )
            return [dict(row) for row in rows]

    def _page_status(self, run_id: str, page_number: int) -> str:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT status FROM pages WHERE run_id=? AND page_number=?",
                (run_id, page_number),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown page {page_number}")
            return cast(str, row["status"])
