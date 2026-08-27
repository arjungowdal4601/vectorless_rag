from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from document_processing.storage import CancelledBeforeCommit, InvalidTransition

from .helpers import commit_input, make_repository


class InjectedCrash(RuntimeError):
    pass


class AtomicRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_crash_after_rename_quarantines_without_adopting(self) -> None:
        repository = make_repository(self.root)

        def crash(step: str) -> None:
            if step == "after_commit_rename":
                raise InjectedCrash(step)

        value = commit_input(repository, fault_hook=crash)
        with self.assertRaises(InjectedCrash):
            repository.commit_page(value)

        report = repository.recover_run("run-1")
        self.assertTrue(report.audit.ok)
        self.assertEqual(report.audit.head_page, 0)
        self.assertEqual(report.reset_page, 1)
        self.assertTrue(report.quarantined)
        page = repository.get_pages("run-1")[0]
        self.assertEqual(page["status"], "pending")
        self.assertIsNone(page["commit_id"])
        self.assertFalse(
            repository.paths_for_run("run-1").page_artifacts_dir.joinpath("page-0001").exists()
        )

    def test_crash_after_database_commit_is_not_reprocessed(self) -> None:
        repository = make_repository(self.root)

        def crash(step: str) -> None:
            if step == "after_db_commit":
                raise InjectedCrash(step)

        with self.assertRaises(InjectedCrash):
            repository.commit_page(commit_input(repository, fault_hook=crash))

        report = repository.recover_run("run-1")
        self.assertTrue(report.audit.ok)
        self.assertEqual(report.audit.head_page, 1)
        self.assertIsNone(report.audit.first_incomplete_page)
        self.assertEqual(repository.get_pages("run-1")[0]["attempt_count"], 1)

    def test_cancelled_commit_leaves_page_pending(self) -> None:
        repository = make_repository(self.root)
        value = commit_input(repository)
        repository.request_cancel("run-1")

        with self.assertRaises(CancelledBeforeCommit):
            repository.commit_page(value)

        page = repository.get_pages("run-1")[0]
        run = repository.get_run("run-1")
        self.assertEqual(page["status"], "pending")
        self.assertIsNone(page["commit_id"])
        self.assertEqual(run["head_page"], 0)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failure_class"], "cancelled")
        self.assertEqual(run["failure_code"], "cancelled")

    def test_in_memory_cancel_check_is_evaluated_at_commit_boundary(self) -> None:
        repository = make_repository(self.root)
        value = replace(commit_input(repository), cancel_check=lambda: True)

        with self.assertRaises(CancelledBeforeCommit):
            repository.commit_page(value)

        self.assertEqual(repository.get_pages("run-1")[0]["status"], "pending")
        self.assertEqual(repository.get_run("run-1")["failure_code"], "cancelled")

    def test_retryable_attempt_returns_page_to_pending(self) -> None:
        repository = make_repository(self.root)
        attempt = repository.begin_page_attempt("run-1", 1, model_id="test:model")

        repository.fail_page_attempt(
            "run-1",
            attempt,
            code="temporary_timeout",
            detail="temporary provider timeout",
            failure_class="transient_model",
            will_retry=True,
        )

        self.assertEqual(repository.get_run("run-1")["status"], "running")
        self.assertEqual(repository.get_pages("run-1")[0]["status"], "pending")
        next_attempt = repository.begin_page_attempt("run-1", 1, model_id="test:model")
        self.assertNotEqual(next_attempt, attempt)

    def test_durable_cancellation_wins_over_page_failure(self) -> None:
        repository = make_repository(self.root)
        attempt = repository.begin_page_attempt("run-1", 1, model_id="test:model")
        repository.request_cancel("run-1")

        with self.assertRaises(CancelledBeforeCommit):
            repository.fail_page_attempt(
                "run-1",
                attempt,
                code="provider_failure",
                detail="must not replace cancellation",
                failure_class="permanent_model",
            )
        repository.mark_run_failed(
            "run-1", "provider_failure", "must still not replace cancellation"
        )

        run = repository.get_run("run-1")
        page = repository.get_page("run-1", 1)
        with repository._db.connect() as connection:
            attempt_row = connection.execute(
                "SELECT status FROM attempts WHERE attempt_id=?", (attempt,)
            ).fetchone()
        self.assertIsNotNone(attempt_row)
        self.assertEqual(run["failure_code"], "cancelled")
        self.assertEqual(page["status"], "pending")
        self.assertEqual(attempt_row["status"], "cancelled")

    def test_resume_clears_only_the_preexisting_cancellation_atomically(self) -> None:
        repository = make_repository(self.root)
        repository.request_cancel("run-1")
        repository.mark_run_failed(
            "run-1", "cancelled", "old cancellation", failure_class="cancelled"
        )

        repository.mark_run_running("run-1", resuming=True)
        resumed = repository.get_run("run-1")
        self.assertEqual(resumed["status"], "resuming")
        self.assertEqual(resumed["cancel_requested"], 0)
        self.assertIsNone(resumed["failure_class"])
        self.assertIsNone(resumed["failure_code"])
        self.assertIsNone(resumed["failure_detail"])

        repository.request_cancel("run-1")
        cancelled = repository.get_run("run-1")
        self.assertEqual(cancelled["status"], "failed")
        self.assertEqual(cancelled["failure_code"], "cancelled")
        with self.assertRaises(InvalidTransition):
            repository.mark_run_running("run-1")

    def test_generic_failure_cannot_overwrite_pending_cancellation(self) -> None:
        repository = make_repository(self.root)
        repository.request_cancel("run-1", "cancel wins")

        repository.mark_run_failed(
            "run-1", "render_failed", "must not win", failure_class="rendering"
        )

        run = repository.get_run("run-1")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failure_class"], "cancelled")
        self.assertEqual(run["failure_code"], "cancelled")

    def test_integrity_failure_dominates_an_old_cancelled_terminal_state(self) -> None:
        repository = make_repository(self.root)
        repository.request_cancel("run-1")
        repository.mark_run_failed("run-1", "cancelled", "cancelled", failure_class="cancelled")
        repository.paths_for_run("run-1").run_manifest.write_text("{}\n", encoding="utf-8")

        recovery = repository.recover_run("run-1")

        self.assertFalse(recovery.audit.ok)
        run = repository.get_run("run-1")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failure_class"], "integrity")
        with self.assertRaises(InvalidTransition):
            repository.mark_run_running("run-1", resuming=True)

    def test_source_copy_crash_is_quarantined_without_adoption(self) -> None:
        repository = make_repository(self.root, run_id="existing")
        repository.create_run("source-crash", {"model": "test:model"})
        upload = self.root / "source-crash.pdf"
        upload.write_bytes(b"%PDF-1.7\nsource crash\n%%EOF\n")

        def crash(step: str) -> None:
            if step == "after_source_fsync":
                raise InjectedCrash(step)

        with self.assertRaises(InjectedCrash):
            repository.store_source("source-crash", upload, fault_hook=crash)

        self.assertFalse(repository.audit_run("source-crash").ok)
        recovery = repository.recover_run("source-crash")
        self.assertTrue(recovery.audit.ok)
        self.assertTrue(recovery.quarantined)
        self.assertFalse(repository.paths_for_run("source-crash").source_pdf.exists())

    def test_pre_transaction_crash_rolls_back_head_and_quarantines(self) -> None:
        repository = make_repository(self.root)

        def crash(step: str) -> None:
            if step == "before_db_commit":
                raise InjectedCrash(step)

        with self.assertRaises(InjectedCrash):
            repository.commit_page(commit_input(repository, fault_hook=crash))

        recovered = repository.recover_run("run-1")
        self.assertTrue(recovered.audit.ok)
        self.assertEqual(recovered.audit.head_page, 0)
        self.assertEqual(repository.get_pages("run-1")[0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
