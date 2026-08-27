"""Run, source, rendering, and attempt lifecycle operations."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .cancellation import RunCancellationMixin
from .commits import PreparedCommit, prepare_commit
from .contract_validation import validate_initial
from .database import Database
from .files import (
    atomic_write,
    canonical_json,
    configuration_fingerprint,
    durable_copy,
    ensure_repository_root,
    ensure_run_directories,
    fsync_directory,
    jsonable,
    paths_for,
    sha256_file,
    validate_run_id,
)
from .models import CommitRecord, FaultHook, InvalidTransition, RunNotFound, RunPaths
from .render_metadata import validate_render_manifest


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class LifecycleMixin(RunCancellationMixin):
    root: Path
    _db: Database

    def initialize(self) -> None:
        ensure_repository_root(self.root, self._db.path)
        self._db.initialize()
        fsync_directory(self.root)

    def paths_for_run(self, run_id: str) -> RunPaths:
        return paths_for(self.root, run_id)

    def create_run(self, run_id: str, config: Any) -> Mapping[str, Any]:
        validate_run_id(run_id)
        config_bytes = canonical_json(config)
        config_text = config_bytes.decode("utf-8")
        config_sha = configuration_fingerprint(config)
        now = utc_now()
        paths = self.paths_for_run(run_id)
        ensure_run_directories(paths)
        run_manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "run_statuses": ["not_started", "running", "completed", "failed", "resuming"],
            "page_statuses": ["pending", "running", "completed", "failed", "skipped"],
            "config": jsonable(config),
            "config_fingerprint": config_sha,
        }
        manifest_bytes = canonical_json(run_manifest)
        if paths.run_manifest.exists() or paths.run_manifest.is_symlink():
            if (
                paths.run_manifest.is_symlink()
                or not paths.run_manifest.is_file()
                or paths.run_manifest.read_bytes() != manifest_bytes
            ):
                raise InvalidTransition("immutable run manifest conflicts with requested run")
        else:
            atomic_write(paths.run_manifest, manifest_bytes)
        try:
            with self._db.immediate() as connection:
                connection.execute(
                    """INSERT INTO runs(
                        run_id,status,phase,config_json,config_sha256,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (run_id, "not_started", "preparing", config_text, config_sha, now, now),
                )
        except sqlite3.IntegrityError as error:
            existing = self.get_run(run_id)
            if existing["config_sha256"] != config_sha:
                raise InvalidTransition(
                    "run id already exists with different configuration"
                ) from error
            return existing
        return self.get_run(run_id)

    def store_source(
        self,
        run_id: str,
        source: Path,
        *,
        original_filename: str | None = None,
        fault_hook: FaultHook | None = None,
    ) -> Mapping[str, Any]:
        self.get_run(run_id)
        if not source.is_file():
            raise FileNotFoundError(source)
        paths = self.paths_for_run(run_id)
        expected_hash = sha256_file(source)
        if paths.source_pdf.exists() or paths.source_pdf.is_symlink():
            if (
                paths.source_pdf.is_symlink()
                or not paths.source_pdf.is_file()
                or sha256_file(paths.source_pdf) != expected_hash
            ):
                raise InvalidTransition("preserved source already exists with different content")
        else:
            durable_copy(source, paths.source_pdf)
        if fault_hook:
            fault_hook("after_source_fsync")
        now = utc_now()
        with self._db.immediate() as connection:
            current = self._require_run(connection, run_id)
            recorded = current["source_sha256"]
            if recorded and recorded != expected_hash:
                raise InvalidTransition("run source is immutable")
            connection.execute(
                """UPDATE runs SET source_path=?,source_sha256=?,original_filename=?,
                   phase='rendering',updated_at=? WHERE run_id=?""",
                ("source/original.pdf", expected_hash, original_filename, now, run_id),
            )
        return self.get_run(run_id)

    def set_render_manifest(self, run_id: str, manifest: Any) -> Mapping[str, Any]:
        run = self.get_run(run_id)
        if not run["source_sha256"]:
            raise InvalidTransition("source must be preserved before rendering metadata")
        validated, page_rows = validate_render_manifest(self.paths_for_run(run_id), manifest, run)
        normalized = validated.model_dump(mode="json")
        count = validated.page_count
        encoded = canonical_json(normalized)
        paths = self.paths_for_run(run_id)
        if paths.render_manifest.exists() or paths.render_manifest.is_symlink():
            if paths.render_manifest.is_symlink() or not paths.render_manifest.is_file():
                raise InvalidTransition("render manifest must be a regular file")
            try:
                stored_payload = json.loads(paths.render_manifest.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise InvalidTransition("stored render manifest is invalid") from error
            if stored_payload != normalized:
                raise InvalidTransition("unrecorded or changed render manifest requires recovery")
            manifest_hash = sha256_file(paths.render_manifest)
        else:
            atomic_write(paths.render_manifest, encoded)
            manifest_hash = sha256_file(paths.render_manifest)
        existing = run["render_manifest_sha256"]
        if existing and existing != manifest_hash:
            raise InvalidTransition("render manifest is immutable after it is recorded")
        now = utc_now()
        with self._db.immediate() as connection:
            current = self._require_run(connection, run_id)
            if current["head_commit_id"] is not None and current["total_pages"] != count:
                raise InvalidTransition("cannot change rendered pages after initialization")
            connection.execute(
                """UPDATE runs SET render_manifest_path='render_manifest.json',
                   render_manifest_sha256=?,total_pages=?,phase='initializing',updated_at=?
                   WHERE run_id=?""",
                (manifest_hash, count, now, run_id),
            )
            for row in page_rows:
                connection.execute(
                    """INSERT INTO pages(run_id,page_number,status,image_path,image_sha256,
                       image_width,image_height) VALUES(?,?,'pending',?,?,?,?)
                       ON CONFLICT(run_id,page_number) DO UPDATE SET
                       image_path=excluded.image_path,image_sha256=excluded.image_sha256,
                       image_width=excluded.image_width,image_height=excluded.image_height""",
                    (run_id, *row),
                )
        return self.get_run(run_id)

    def initialize_artifacts(
        self, run_id: str, short_term_memory: Any, document_structure: Any
    ) -> CommitRecord:
        initial_memory, initial_structure = validate_initial(short_term_memory, document_structure)
        run = self.get_run(run_id)
        if run["head_commit_id"]:
            row = self._get_commit_row(run_id, 0)
            return self._commit_record(row)
        if not run["source_sha256"] or not run["render_manifest_sha256"]:
            raise InvalidTransition("source and renders must exist before initialization")
        prepared = prepare_commit(
            paths=self.paths_for_run(run_id),
            run_id=run_id,
            page_number=0,
            previous_commit_id=None,
            config_sha256=run["config_sha256"],
            source_sha256=run["source_sha256"],
            image_path=None,
            image_sha256=None,
            short_term_memory=initial_memory,
            document_structure=initial_structure,
        )
        now = utc_now()
        with self._db.immediate() as connection:
            current = self._require_run(connection, run_id)
            if current["head_commit_id"] is not None:
                raise InvalidTransition("run was initialized concurrently")
            self._insert_commit(connection, prepared, None, now)
            connection.execute(
                """UPDATE runs SET head_commit_id=?,head_page=0,phase='ready',updated_at=?
                   WHERE run_id=?""",
                (prepared.record.commit_id, now, run_id),
            )
        return prepared.record

    def mark_run_running(self, run_id: str, *, resuming: bool = False) -> None:
        with self._db.immediate() as connection:
            run = self._require_run(connection, run_id)
            if resuming and run["status"] == "failed":
                if run["failure_class"] == "integrity":
                    raise InvalidTransition("integrity failures cannot be resumed")
                connection.execute(
                    """UPDATE runs SET status='resuming',phase='recovering',
                       cancel_requested=0,failure_class=NULL,failure_code=NULL,
                       failure_detail=NULL,final_manifest_path=NULL,
                       final_manifest_sha256=NULL,completed_at=NULL,updated_at=?
                       WHERE run_id=?""",
                    (utc_now(), run_id),
                )
                return
            allowed = set() if resuming else {"not_started", "running", "resuming"}
            if run["status"] not in allowed or run["cancel_requested"]:
                target = "resuming" if resuming else "running"
                raise InvalidTransition(f"cannot enter {target} from the current run state")
            phase = (
                "recovering"
                if resuming
                else ("processing" if run["head_commit_id"] else run["phase"])
            )
            connection.execute(
                "UPDATE runs SET status=?,phase=?,updated_at=? WHERE run_id=?",
                ("resuming" if resuming else "running", phase, utc_now(), run_id),
            )

    def get_run(self, run_id: str) -> Mapping[str, Any]:
        with self._db.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            return dict(row)

    def list_runs(self) -> list[Mapping[str, Any]]:
        with self._db.connect() as connection:
            return [
                dict(row)
                for row in connection.execute("SELECT * FROM runs ORDER BY created_at,rowid")
            ]

    def get_pages(self, run_id: str) -> list[Mapping[str, Any]]:
        self.get_run(run_id)
        with self._db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pages WHERE run_id=? ORDER BY page_number", (run_id,)
            )
            return [dict(row) for row in rows]

    def get_page(self, run_id: str, page_number: int) -> Mapping[str, Any]:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM pages WHERE run_id=? AND page_number=?",
                (run_id, page_number),
            ).fetchone()
            if row is None:
                raise RunNotFound(f"page {run_id}/{page_number}")
            return dict(row)

    def _require_run(self, connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        return cast(sqlite3.Row, row)

    def _get_commit_row(self, run_id: str, page_number: int) -> sqlite3.Row:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM commits WHERE run_id=? AND page_number=?",
                (run_id, page_number),
            ).fetchone()
            if row is None:
                raise RunNotFound(f"commit {run_id}/{page_number}")
            return cast(sqlite3.Row, row)

    @staticmethod
    def _commit_record(row: sqlite3.Row) -> CommitRecord:
        relative = row["relative_path"]
        page = row["page_number"]
        return CommitRecord(
            commit_id=row["commit_id"],
            run_id=row["run_id"],
            page_number=page,
            relative_path=relative,
            manifest_sha256=row["manifest_sha256"],
            page_json_path=(f"{relative}/page.json" if page else None),
            short_term_memory_path=f"{relative}/short_term_memory.json",
            document_structure_path=f"{relative}/document_structure.json",
        )

    @staticmethod
    def _insert_commit(
        connection: sqlite3.Connection,
        prepared: PreparedCommit,
        attempt_id: str | None,
        now: str,
    ) -> None:
        record = prepared.record
        connection.execute(
            """INSERT INTO commits(commit_id,run_id,page_number,previous_commit_id,
               relative_path,manifest_sha256,page_json_sha256,memory_sha256,
               structure_sha256,attempt_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record.commit_id,
                record.run_id,
                record.page_number,
                prepared.manifest["previous_commit_id"],
                record.relative_path,
                record.manifest_sha256,
                prepared.files.get("page.json"),
                prepared.files["short_term_memory.json"],
                prepared.files["document_structure.json"],
                attempt_id,
                now,
            ),
        )
