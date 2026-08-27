from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from .helpers import commit_input, completed_manifest, make_repository


class ReferencedLeafSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_run_manifest_symlink_fails_audit(self) -> None:
        repository = make_repository(self.root)
        manifest = repository.paths_for_run("run-1").run_manifest
        outside = self.root / "outside-run.json"
        manifest.rename(outside)
        manifest.symlink_to(outside)

        report = repository.audit_run("run-1")

        self.assertFalse(report.ok)
        self.assertTrue(any(issue.code == "run_manifest_invalid" for issue in report.issues))

    def test_final_special_file_is_rejected_without_reading(self) -> None:
        repository = make_repository(self.root)
        repository.commit_page(commit_input(repository))
        final = repository.prepare_final_manifest("run-1", completed_manifest(repository))
        repository.finalize_run(final)
        usage = repository.paths_for_run("run-1").final_dir / "model_usage.json"
        usage.unlink()
        os.mkfifo(usage)

        report = repository.audit_run("run-1")

        self.assertFalse(report.ok)
        self.assertTrue(any(issue.code == "final_output_invalid" for issue in report.issues))


if __name__ == "__main__":
    unittest.main()
