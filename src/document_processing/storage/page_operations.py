"""Per-page attempt and atomic commit operations."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Mapping
from typing import Any, cast

from .commits import PreparedCommit, prepare_commit, verify_commit
from .contract_validation import validate_page_commit, validate_prior_state_json
from .files import canonical_json, jsonable, quarantine, resolve_artifact
from .lifecycle import LifecycleMixin, utc_now
from .models import (
    CancelledBeforeCommit,
    CommitRecord,
    IntegrityError,
    InvalidTransition,
    PageCommitInput,
)

PAGE_FIELDS = {
    "page_number",
    "page_type",
    "page_image_path",
    "index_decision",
    "index_reason",
    "summary",
    "topics",
    "assets",
}


class PageOperationsMixin(LifecycleMixin):
    def begin_page_attempt(
        self,
        run_id: str,
        page_number: int,
        *,
        model_id: str | None = None,
        request_fingerprint: str | None = None,
    ) -> str:
        attempt_id = uuid.uuid4().hex
        now = utc_now()
        with self._db.immediate() as connection:
            run = self._require_run(connection, run_id)
            if run["status"] != "running":
                raise InvalidTransition("page work requires a running document run")
            if not run["head_commit_id"]:
                raise InvalidTransition("page work requires initialized state artifacts")
            if run["cancel_requested"]:
                raise CancelledBeforeCommit(run_id)
            if page_number != run["head_page"] + 1:
                raise InvalidTransition("pages must be processed in contiguous order")
            page = self._require_page(connection, run_id, page_number)
            if page["status"] != "pending":
                raise InvalidTransition(f"page {page_number} is {page['status']}")
            ordinal = page["attempt_count"] + 1
            connection.execute(
                """INSERT INTO attempts(attempt_id,run_id,page_number,ordinal,status,
                   model_id,request_fingerprint,started_at) VALUES(?,?,?,?,'running',?,?,?)""",
                (attempt_id, run_id, page_number, ordinal, model_id, request_fingerprint, now),
            )
            connection.execute(
                """UPDATE pages SET status='running',active_attempt_id=?,attempt_count=?,
                   started_at=?,error_class=NULL,error_code=NULL,error_detail=NULL
                   WHERE run_id=? AND page_number=?""",
                (attempt_id, ordinal, now, run_id, page_number),
            )
        return attempt_id

    def commit_page(self, value: PageCommitInput) -> CommitRecord:
        run = self.get_run(value.run_id)
        page_row = self._page(value.run_id, value.page_number)
        page_payload = jsonable(value.page)
        self._validate_page_payload(page_payload, value.page_number, page_row["image_path"])
        prior_memory, prior_structure = self._load_prior_state(run, page_number=value.page_number)
        validated = validate_page_commit(
            page_number=value.page_number,
            image_path=page_row["image_path"],
            page=page_payload,
            short_term_memory=value.short_term_memory,
            document_structure=value.document_structure,
            model_response=value.model_response,
            usage=value.usage,
            prior_short_term_memory=prior_memory,
            prior_document_structure=prior_structure,
        )
        prepared = prepare_commit(
            paths=self.paths_for_run(value.run_id),
            run_id=value.run_id,
            page_number=value.page_number,
            previous_commit_id=run["head_commit_id"],
            config_sha256=run["config_sha256"],
            source_sha256=run["source_sha256"],
            image_path=page_row["image_path"],
            image_sha256=page_row["image_sha256"],
            short_term_memory=validated.short_term_memory,
            document_structure=validated.document_structure,
            page=validated.page,
            model_response=validated.model_response,
            usage=validated.usage,
            attempt_id=value.attempt_id,
            fault_hook=value.fault_hook,
        )
        cancelled = False
        now = utc_now()
        with self._db.immediate() as connection:
            current = self._require_run(connection, value.run_id)
            page = self._require_page(connection, value.run_id, value.page_number)
            attempt = self._require_attempt(connection, value.attempt_id)
            self._validate_commit_claim(current, page, attempt, value, prepared, validated.usage)
            if current["cancel_requested"] or (
                value.cancel_check is not None and value.cancel_check()
            ):
                self._discard_in_transaction(
                    connection, value.run_id, value.attempt_id, "cancelled_before_commit"
                )
                cancelled = True
            else:
                self._insert_commit(connection, prepared, value.attempt_id, now)
                usage_text = canonical_json(validated.usage).decode("utf-8")
                connection.execute(
                    """UPDATE attempts SET status='completed',usage_json=?,finished_at=?
                       WHERE attempt_id=?""",
                    (usage_text, now, value.attempt_id),
                )
                connection.execute(
                    """UPDATE pages SET status='completed',active_attempt_id=NULL,commit_id=?,
                       page_json_path=?,page_json_sha256=?,completed_at=?
                       WHERE run_id=? AND page_number=?""",
                    (
                        prepared.record.commit_id,
                        prepared.record.page_json_path,
                        prepared.files["page.json"],
                        now,
                        value.run_id,
                        value.page_number,
                    ),
                )
                connection.execute(
                    """UPDATE runs SET head_page=?,head_commit_id=?,phase='processing',
                       updated_at=? WHERE run_id=?""",
                    (value.page_number, prepared.record.commit_id, now, value.run_id),
                )
                if value.fault_hook:
                    value.fault_hook("before_db_commit")
        if cancelled:
            paths = self.paths_for_run(value.run_id)
            quarantine(prepared.path, paths.quarantine_dir, "cancelled-commit")
            raise CancelledBeforeCommit(value.run_id)
        if value.fault_hook:
            value.fault_hook("after_db_commit")
        return prepared.record

    def discard_page_attempt(self, run_id: str, attempt_id: str, reason: str) -> None:
        with self._db.immediate() as connection:
            self._require_run(connection, run_id)
            self._discard_in_transaction(connection, run_id, attempt_id, reason)

    def fail_page_attempt(
        self,
        run_id: str,
        attempt_id: str,
        *,
        code: str,
        detail: str,
        failure_class: str = "permanent",
        will_retry: bool = False,
        usage: Any = None,
    ) -> None:
        now = utc_now()
        cancelled = False
        with self._db.immediate() as connection:
            run = self._require_run(connection, run_id)
            attempt = self._require_attempt(connection, attempt_id)
            if attempt["run_id"] != run_id or attempt["status"] != "running":
                raise InvalidTransition("attempt is not active for this run")
            if run["cancel_requested"]:
                self._discard_in_transaction(
                    connection, run_id, attempt_id, "cancelled_during_attempt_failure"
                )
                cancelled = True
            else:
                connection.execute(
                    """UPDATE attempts SET status='failed',failure_class=?,failure_code=?,
                       failure_detail=?,usage_json=?,finished_at=? WHERE attempt_id=?""",
                    (
                        failure_class,
                        code,
                        detail,
                        canonical_json(usage).decode("utf-8") if usage is not None else None,
                        now,
                        attempt_id,
                    ),
                )
                connection.execute(
                    """UPDATE pages SET status=?,active_attempt_id=NULL,error_class=?,
                       error_code=?,error_detail=? WHERE run_id=? AND page_number=?""",
                    (
                        "pending" if will_retry else "failed",
                        failure_class,
                        code,
                        detail,
                        run_id,
                        attempt["page_number"],
                    ),
                )
                if will_retry:
                    connection.execute("UPDATE runs SET updated_at=? WHERE run_id=?", (now, run_id))
                else:
                    connection.execute(
                        """UPDATE runs SET status='failed',phase='failed',failure_class=?,
                           failure_code=?,failure_detail=?,updated_at=? WHERE run_id=?""",
                        (failure_class, code, detail, now, run_id),
                    )
        if cancelled:
            raise CancelledBeforeCommit(run_id)

    def requeue_failed_page(self, run_id: str, page_number: int) -> None:
        with self._db.immediate() as connection:
            run = self._require_run(connection, run_id)
            page = self._require_page(connection, run_id, page_number)
            if run["status"] not in {"failed", "resuming"} or page["status"] != "failed":
                raise InvalidTransition("only failed pages in failed/resuming runs may be requeued")
            if page_number != run["head_page"] + 1:
                raise InvalidTransition("only the first incomplete page may be requeued")
            connection.execute(
                """UPDATE pages SET status='pending',error_class=NULL,error_code=NULL,
                   error_detail=NULL WHERE run_id=? AND page_number=?""",
                (run_id, page_number),
            )

    def _discard_in_transaction(
        self, connection: sqlite3.Connection, run_id: str, attempt_id: str, reason: str
    ) -> None:
        attempt = self._require_attempt(connection, attempt_id)
        if attempt["run_id"] != run_id or attempt["status"] != "running":
            raise InvalidTransition("attempt is not active for this run")
        now = utc_now()
        connection.execute(
            """UPDATE attempts SET status='cancelled',failure_class='cancelled',
               failure_code=?,finished_at=? WHERE attempt_id=?""",
            (reason, now, attempt_id),
        )
        connection.execute(
            """UPDATE pages SET status='pending',active_attempt_id=NULL
               WHERE run_id=? AND page_number=?""",
            (run_id, attempt["page_number"]),
        )
        connection.execute(
            """UPDATE runs SET status='failed',phase='failed',failure_class='cancelled',
               failure_code='cancelled',failure_detail=?,updated_at=? WHERE run_id=?""",
            (reason, now, run_id),
        )

    @staticmethod
    def _validate_page_payload(value: Any, number: int, image_path: str) -> None:
        if not isinstance(value, Mapping) or set(value) != PAGE_FIELDS:
            raise ValueError("page artifact must contain exactly the contracted top-level fields")
        if value["page_number"] != number or value["page_image_path"] != image_path:
            raise ValueError("page artifact number/image reference does not match durable metadata")

    @staticmethod
    def _validate_commit_claim(
        run: sqlite3.Row,
        page: sqlite3.Row,
        attempt: sqlite3.Row,
        value: PageCommitInput,
        prepared: PreparedCommit,
        usage_value: Any,
    ) -> None:
        if run["status"] != "running" or run["head_page"] + 1 != value.page_number:
            raise InvalidTransition("run head changed before page commit")
        if run["head_commit_id"] != prepared.manifest["previous_commit_id"]:
            raise InvalidTransition("previous commit changed before page commit")
        if page["status"] != "running" or page["active_attempt_id"] != value.attempt_id:
            raise InvalidTransition("page attempt no longer owns the page")
        if attempt["status"] != "running" or attempt["run_id"] != value.run_id:
            raise InvalidTransition("attempt is not active")
        usage = jsonable(usage_value)
        if usage.get("attempt_number") != attempt["ordinal"]:
            raise InvalidTransition("usage attempt number does not match the active attempt")
        if attempt["model_id"] and usage.get("model_id") != attempt["model_id"]:
            raise InvalidTransition("usage model id does not match the active attempt")

    def _page(self, run_id: str, page_number: int) -> Mapping[str, Any]:
        with self._db.connect() as connection:
            return dict(self._require_page(connection, run_id, page_number))

    def _load_prior_state(self, run: Mapping[str, Any], *, page_number: int) -> tuple[Any, Any]:
        prior_page = page_number - 1
        row = self._get_commit_row(run["run_id"], prior_page)
        expected_relative = (
            "initial" if prior_page == 0 else f"page_artifacts/page-{prior_page:04d}"
        )
        if row["commit_id"] != run["head_commit_id"] or row["relative_path"] != expected_relative:
            raise IntegrityError("run head does not identify the expected prior snapshot")
        directory = resolve_artifact(self.paths_for_run(run["run_id"]).root, expected_relative)
        manifest = verify_commit(directory, row["commit_id"])
        expected_metadata = {
            "run_id": run["run_id"],
            "page_number": prior_page,
            "config_sha256": run["config_sha256"],
            "source_sha256": run["source_sha256"],
        }
        if any(manifest.get(key) != value for key, value in expected_metadata.items()):
            raise IntegrityError("prior commit metadata differs from the durable run")
        files = manifest.get("files", {})
        if (
            files.get("short_term_memory.json") != row["memory_sha256"]
            or files.get("document_structure.json") != row["structure_sha256"]
        ):
            raise IntegrityError("prior snapshot hashes differ from the commit ledger")
        return validate_prior_state_json(
            page_number=page_number,
            memory=(directory / "short_term_memory.json").read_bytes(),
            structure=(directory / "document_structure.json").read_bytes(),
        )

    @staticmethod
    def _require_page(connection: sqlite3.Connection, run_id: str, page_number: int) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM pages WHERE run_id=? AND page_number=?", (run_id, page_number)
        ).fetchone()
        if row is None:
            raise InvalidTransition(f"unknown page {page_number}")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _require_attempt(connection: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise InvalidTransition(f"unknown attempt {attempt_id}")
        return cast(sqlite3.Row, row)
