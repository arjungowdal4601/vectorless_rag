from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from document_processing.storage import InvalidTransition, RunRepository

from .helpers import commit_input, completed_manifest, make_repository


class ResumeLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_claimed_resume_survives_recovery_until_worker_activation(self) -> None:
        repository = make_repository(self.root)
        repository.mark_run_failed(
            "run-1", "provider_failed", "retry later", failure_class="transient_model"
        )

        repository.mark_run_running("run-1", resuming=True)
        recovery = repository.recover_run("run-1")

        self.assertTrue(recovery.audit.ok)
        self.assertEqual(repository.get_run("run-1")["status"], "resuming")
        with self.assertRaises(InvalidTransition):
            repository.mark_run_running("run-1", resuming=True)
        repository.mark_run_running("run-1")
        self.assertEqual(repository.get_run("run-1")["status"], "running")

    def test_startup_claim_is_atomic_with_later_cancellation(self) -> None:
        repository = make_repository(self.root)
        self.assertTrue(repository.recover_run("run-1").audit.ok)

        claimed = repository.claim_recovered_run("run-1")
        repository.request_cancel("run-1", "cancelled after startup claim")

        self.assertEqual(claimed["status"], "resuming")
        cancelled = repository.get_run("run-1")
        self.assertEqual(cancelled["status"], "failed")
        self.assertEqual(cancelled["failure_code"], "cancelled")
        with self.assertRaises(InvalidTransition):
            repository.mark_run_running("run-1")

    def test_pre_render_failed_run_can_be_claimed_and_audited(self) -> None:
        repository = RunRepository(self.root / "var" / "document-processing")
        repository.initialize()
        repository.create_run("pre-render", {"model": "test:model"})
        repository.mark_run_failed(
            "pre-render", "preparation_failed", "retry", failure_class="storage"
        )

        repository.mark_run_running("pre-render", resuming=True)
        recovery = repository.recover_run("pre-render")

        self.assertTrue(recovery.audit.ok)
        self.assertEqual(repository.get_run("pre-render")["status"], "resuming")
        rolled_back = repository.fail_resuming_run(
            "pre-render",
            "source_unavailable",
            "The source is no longer available.",
            failure_class="storage",
        )
        self.assertEqual(rolled_back["status"], "failed")
        self.assertEqual(rolled_back["failure_code"], "source_unavailable")

    def test_cancel_wins_over_resume_rollback(self) -> None:
        repository = make_repository(self.root)
        repository.mark_run_failed("run-1", "retry", "retry", failure_class="transient_model")
        repository.mark_run_running("run-1", resuming=True)

        repository.request_cancel("run-1", "cancelled after claim")
        unchanged = repository.fail_resuming_run(
            "run-1", "resume_rejected", "must not win", failure_class="configuration"
        )

        self.assertEqual(unchanged["status"], "failed")
        self.assertEqual(unchanged["failure_class"], "cancelled")
        self.assertEqual(unchanged["failure_code"], "cancelled")
        self.assertEqual(repository.get_run("run-1")["failure_detail"], "cancelled after claim")

    def test_failed_page_is_requeued_while_resume_claim_is_held(self) -> None:
        repository = make_repository(self.root)
        attempt = repository.begin_page_attempt("run-1", 1, model_id="test:model")
        repository.fail_page_attempt(
            "run-1", attempt, code="provider_failed", detail="retry on resume"
        )
        repository.mark_run_running("run-1", resuming=True)

        recovery = repository.recover_run("run-1")
        repository.requeue_failed_page("run-1", 1)

        self.assertTrue(recovery.audit.ok)
        self.assertEqual(repository.get_page("run-1", 1)["status"], "pending")
        self.assertEqual(repository.get_run("run-1")["status"], "resuming")

    def test_after_final_database_commit_is_authoritative(self) -> None:
        repository = make_repository(self.root)
        repository.commit_page(commit_input(repository))
        final = repository.prepare_final_manifest("run-1", completed_manifest(repository))

        def crash(step: str) -> None:
            if step == "after_final_db_commit":
                raise RuntimeError(step)

        with self.assertRaisesRegex(RuntimeError, "after_final_db_commit"):
            repository.finalize_run(final, fault_hook=crash)

        completed = repository.get_run("run-1")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["final_manifest_sha256"], final.sha256)
        self.assertTrue(repository.audit_run("run-1").ok)

    def test_list_runs_breaks_timestamp_ties_by_durable_insertion_order(self) -> None:
        repository = RunRepository(self.root / "fifo-artifacts")
        repository.initialize()
        repository.create_run("z-first", {"model": "test:model"})
        repository.create_run("a-second", {"model": "test:model"})
        with repository._db.immediate() as connection:
            connection.execute(
                "UPDATE runs SET created_at='same' WHERE run_id IN (?,?)",
                (
                    "z-first",
                    "a-second",
                ),
            )

        self.assertEqual(
            [row["run_id"] for row in repository.list_runs()],
            ["z-first", "a-second"],
        )


if __name__ == "__main__":
    unittest.main()
