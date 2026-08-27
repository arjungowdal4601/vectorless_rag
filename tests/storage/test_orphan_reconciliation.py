from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from document_processing.storage import RunRepository

from .helpers import commit_input, completed_manifest, make_repository


class OrphanReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _inject_orphan(repository: RunRepository) -> Path:
        paths = repository.paths_for_run("run-1")
        orphan = paths.page_artifacts_dir / "page-9999"
        orphan.mkdir()
        (orphan / "junk").write_bytes(b"unreferenced")
        return orphan

    def test_finalization_quarantines_unreferenced_commit_directory(self) -> None:
        repository = make_repository(self.root)
        repository.commit_page(commit_input(repository))
        orphan = self._inject_orphan(repository)
        self.assertFalse(repository.audit_run("run-1").ok)

        final = repository.prepare_final_manifest("run-1", completed_manifest(repository))
        completed = repository.finalize_run(final)

        self.assertEqual(completed["status"], "completed")
        self.assertFalse(orphan.exists())
        self.assertTrue(repository.audit_run("run-1").ok)

    def test_completed_integrity_check_heals_orphans_without_demotion(self) -> None:
        repository = make_repository(self.root)
        repository.commit_page(commit_input(repository))
        final = repository.prepare_final_manifest("run-1", completed_manifest(repository))
        repository.finalize_run(final)
        orphan = self._inject_orphan(repository)

        healed = repository.mark_completed_integrity_failed("run-1")

        self.assertEqual(healed["status"], "completed")
        self.assertFalse(orphan.exists())
        self.assertTrue(repository.audit_run("run-1").ok)

    def test_completed_recovery_quarantines_unknown_run_root_directory(self) -> None:
        repository = make_repository(self.root)
        repository.commit_page(commit_input(repository))
        final = repository.prepare_final_manifest("run-1", completed_manifest(repository))
        repository.finalize_run(final)
        rogue = repository.paths_for_run("run-1").root / "rogue"
        rogue.mkdir()
        (rogue / "data").write_bytes(b"unknown")

        initial = repository.audit_run("run-1")
        recovery = repository.recover_run("run-1")

        self.assertFalse(initial.ok)
        self.assertIn("rogue", initial.orphan_paths)
        self.assertTrue(recovery.audit.ok)
        self.assertFalse(rogue.exists())


if __name__ == "__main__":
    unittest.main()
