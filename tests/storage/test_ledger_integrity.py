from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from document_processing.storage import IntegrityError, RunRepository
from document_processing.storage.files import canonical_json, sha256_bytes

from .helpers import commit_input, completed_manifest, make_repository


class LedgerIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_commit_ledger_predecessor_must_match_verified_chain(self) -> None:
        repository = make_repository(self.root)
        repository.commit_page(commit_input(repository))
        with repository._db.immediate() as connection:
            connection.execute(
                """UPDATE commits SET previous_commit_id='deadbeef'
                   WHERE run_id='run-1' AND page_number=1"""
            )

        report = repository.audit_run("run-1")

        self.assertFalse(report.ok)
        self.assertTrue(any("predecessor" in issue.message for issue in report.issues))

    def test_page_commit_requires_the_exact_file_hash_set(self) -> None:
        repository = make_repository(self.root)
        repository.commit_page(commit_input(repository))
        directory = repository.paths_for_run("run-1").page_artifacts_dir / "page-0001"
        manifest_path = directory / "commit.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        del manifest["files"]["usage.json"]
        manifest_bytes = canonical_json(manifest)
        manifest_path.write_bytes(manifest_bytes)
        self._replace_head_commit_id(repository, sha256_bytes(manifest_bytes))

        report = repository.audit_run("run-1")

        self.assertFalse(report.ok)
        self.assertTrue(any(issue.code == "commit_invalid" for issue in report.issues))

    def test_deleted_completed_attempt_fails_audit_and_finalization(self) -> None:
        repository = make_repository(self.root)
        value = commit_input(repository)
        repository.commit_page(value)
        with repository._db.immediate() as connection:
            connection.execute("DELETE FROM attempts WHERE attempt_id=?", (value.attempt_id,))

        report = repository.audit_run("run-1")

        self.assertFalse(report.ok)
        self.assertTrue(any(issue.code == "attempt_ledger_invalid" for issue in report.issues))
        with self.assertRaises(IntegrityError):
            repository.prepare_final_manifest("run-1", completed_manifest(repository))

    def test_attempt_usage_must_equal_the_immutable_commit(self) -> None:
        repository = make_repository(self.root)
        value = commit_input(repository)
        repository.commit_page(value)
        changed_usage = {**value.usage, "provider_request_id": "different-request"}
        with repository._db.immediate() as connection:
            connection.execute(
                "UPDATE attempts SET usage_json=? WHERE attempt_id=?",
                (canonical_json(changed_usage).decode(), value.attempt_id),
            )

        report = repository.audit_run("run-1")

        self.assertFalse(report.ok)
        self.assertTrue(any("usage differs" in issue.message for issue in report.issues))

    def test_full_run_is_reaudited_at_the_terminal_commit_boundary(self) -> None:
        repository = make_repository(self.root)
        repository.commit_page(commit_input(repository))
        final = repository.prepare_final_manifest("run-1", completed_manifest(repository))
        image = repository.paths_for_run("run-1").page_images_dir / "page-0001.png"
        image.write_bytes(b"tampered after final preparation")

        with self.assertRaises(IntegrityError):
            repository.finalize_run(final)

        self.assertEqual(repository.get_run("run-1")["status"], "running")

    @staticmethod
    def _replace_head_commit_id(repository: RunRepository, commit_id: str) -> None:
        with repository._db.immediate() as connection:
            connection.execute(
                """UPDATE commits SET commit_id=?,manifest_sha256=?
                   WHERE run_id='run-1' AND page_number=1""",
                (commit_id, commit_id),
            )
            connection.execute(
                "UPDATE pages SET commit_id=? WHERE run_id='run-1' AND page_number=1",
                (commit_id,),
            )
            connection.execute(
                "UPDATE runs SET head_commit_id=? WHERE run_id='run-1'",
                (commit_id,),
            )


if __name__ == "__main__":
    unittest.main()
