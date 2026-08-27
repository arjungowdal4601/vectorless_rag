from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from document_processing.storage import RunRepository

from .helpers import make_repository


class AttemptIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _assert_attempt_issue(self, repository: RunRepository) -> None:
        report = repository.audit_run("run-1")
        self.assertFalse(report.ok)
        self.assertTrue(any(issue.code == "attempt_state_invalid" for issue in report.issues))

    def test_multiple_running_pages_fail_closed_during_recovery(self) -> None:
        repository = make_repository(self.root, page_count=2)
        repository.begin_page_attempt("run-1", 1)
        second_id = uuid.uuid4().hex
        with repository._db.immediate() as connection:
            connection.execute(
                """INSERT INTO attempts(attempt_id,run_id,page_number,ordinal,status,started_at)
                   VALUES(?,'run-1',2,1,'running','now')""",
                (second_id,),
            )
            connection.execute(
                """UPDATE pages SET status='running',active_attempt_id=?,attempt_count=1
                   WHERE run_id='run-1' AND page_number=2""",
                (second_id,),
            )

        self._assert_attempt_issue(repository)
        repository.recover_run("run-1")
        self.assertEqual(repository.get_run("run-1")["failure_class"], "integrity")

    def test_null_or_mismatched_active_attempt_fails_audit(self) -> None:
        for mismatch in (None, uuid.uuid4().hex):
            with self.subTest(active_attempt_id=mismatch):
                repository = make_repository(self.root / str(mismatch))
                repository.begin_page_attempt("run-1", 1)
                with repository._db.immediate() as connection:
                    connection.execute(
                        """UPDATE pages SET active_attempt_id=?
                           WHERE run_id='run-1' AND page_number=1""",
                        (mismatch,),
                    )
                self._assert_attempt_issue(repository)

    def test_stray_running_attempt_on_pending_page_fails_audit(self) -> None:
        repository = make_repository(self.root)
        repository.begin_page_attempt("run-1", 1)
        with repository._db.immediate() as connection:
            connection.execute(
                """UPDATE pages SET status='pending',active_attempt_id=NULL
                   WHERE run_id='run-1' AND page_number=1"""
            )

        self._assert_attempt_issue(repository)

    def test_attempt_ordinal_must_match_page_attempt_count(self) -> None:
        repository = make_repository(self.root)
        attempt = repository.begin_page_attempt("run-1", 1)
        repository.fail_page_attempt(
            "run-1", attempt, code="retry", detail="retry", will_retry=True
        )
        with repository._db.immediate() as connection:
            connection.execute(
                """UPDATE pages SET attempt_count=2
                   WHERE run_id='run-1' AND page_number=1"""
            )

        self._assert_attempt_issue(repository)


if __name__ == "__main__":
    unittest.main()
