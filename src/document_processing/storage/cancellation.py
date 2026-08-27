"""Atomic run cancellation and terminal failure transitions."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .database import Database
from .models import InvalidTransition


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class RunCancellationMixin:
    _db: Database

    def _require_run(self, connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        raise NotImplementedError

    def mark_run_failed(
        self, run_id: str, code: str, detail: str, *, failure_class: str = "permanent"
    ) -> None:
        with self._db.immediate() as connection:
            run = self._require_run(connection, run_id)
            if run["status"] == "completed":
                return
            existing_cancelled = (
                run["failure_class"] == "cancelled" or run["failure_code"] == "cancelled"
            )
            incoming_cancelled = failure_class == "cancelled" or code == "cancelled"
            if run["status"] == "failed" and existing_cancelled and not incoming_cancelled:
                return
            if run["cancel_requested"] and not incoming_cancelled:
                connection.execute(
                    """UPDATE runs SET status='failed',phase='failed',
                       failure_class='cancelled',failure_code='cancelled',
                       failure_detail='cancel_requested_before_failure',updated_at=?
                       WHERE run_id=?""",
                    (_utc_now(), run_id),
                )
                return
            connection.execute(
                """UPDATE runs SET status='failed',phase='failed',failure_class=?,
                   failure_code=?,failure_detail=?,updated_at=? WHERE run_id=?""",
                (failure_class, code, detail, _utc_now(), run_id),
            )

    def mark_run_integrity_failed(self, run_id: str, code: str, detail: str) -> Mapping[str, Any]:
        """Fail closed on verified corruption, even after an earlier cancellation."""

        with self._db.immediate() as connection:
            run = self._require_run(connection, run_id)
            if run["status"] == "completed":
                return dict(run)
            connection.execute(
                """UPDATE runs SET status='failed',phase='failed',
                   failure_class='integrity',failure_code=?,failure_detail=?,updated_at=?
                   WHERE run_id=?""",
                (code, detail, _utc_now(), run_id),
            )
            return dict(self._require_run(connection, run_id))

    def fail_resuming_run(
        self,
        run_id: str,
        code: str,
        detail: str,
        *,
        failure_class: str,
    ) -> Mapping[str, Any]:
        """Conditionally roll back a resume claim without overwriting a terminal race."""

        with self._db.immediate() as connection:
            run = self._require_run(connection, run_id)
            if run["status"] in {"completed", "failed"}:
                return dict(run)
            if run["status"] != "resuming":
                raise InvalidTransition("only a resuming run can roll back its resume claim")
            connection.execute(
                """UPDATE runs SET status='failed',phase='failed',failure_class=?,
                   failure_code=?,failure_detail=?,updated_at=? WHERE run_id=?""",
                (failure_class, code, detail, _utc_now(), run_id),
            )
            return dict(self._require_run(connection, run_id))

    def claim_recovered_run(self, run_id: str) -> Mapping[str, Any]:
        """Atomically claim audited startup work without clearing cancellation state."""

        with self._db.immediate() as connection:
            run = self._require_run(connection, run_id)
            if run["status"] in {"completed", "failed"}:
                return dict(run)
            if run["cancel_requested"]:
                connection.execute(
                    """UPDATE runs SET status='failed',phase='failed',
                       failure_class='cancelled',failure_code='cancelled',
                       failure_detail='cancel_requested_before_startup_claim',updated_at=?
                       WHERE run_id=?""",
                    (_utc_now(), run_id),
                )
            elif run["status"] in {"not_started", "running", "resuming"}:
                connection.execute(
                    """UPDATE runs SET status='resuming',phase='recovering',updated_at=?
                       WHERE run_id=?""",
                    (_utc_now(), run_id),
                )
            else:
                raise InvalidTransition("run cannot be claimed for startup recovery")
            return dict(self._require_run(connection, run_id))

    def claim_interrupted_run(self, run_id: str) -> Mapping[str, Any]:
        """Backward-compatible name for the atomic startup recovery claim."""

        return self.claim_recovered_run(run_id)

    def request_cancel(
        self,
        run_id: str,
        detail: str = "Document processing was cancelled.",
    ) -> Mapping[str, Any]:
        """Atomically cancel queued work or flag active work at its next boundary."""

        with self._db.immediate() as connection:
            run = self._require_run(connection, run_id)
            if run["status"] in {"completed", "failed"}:
                return dict(run)
            now = _utc_now()
            if run["status"] in {"not_started", "resuming"}:
                connection.execute(
                    """UPDATE runs SET status='failed',phase='failed',cancel_requested=1,
                       failure_class='cancelled',failure_code='cancelled',failure_detail=?,
                       updated_at=? WHERE run_id=?""",
                    (detail, now, run_id),
                )
            else:
                connection.execute(
                    "UPDATE runs SET cancel_requested=1,updated_at=? WHERE run_id=?",
                    (now, run_id),
                )
            updated = self._require_run(connection, run_id)
            return dict(updated)

    def is_cancel_requested(self, run_id: str) -> bool:
        return bool(self.get_run(run_id)["cancel_requested"])

    def get_run(self, run_id: str) -> Mapping[str, Any]:
        raise NotImplementedError

    def clear_cancel(self, run_id: str) -> None:
        raise InvalidTransition(
            "cancellation can only be cleared by an atomic failed-to-resuming claim"
        )
