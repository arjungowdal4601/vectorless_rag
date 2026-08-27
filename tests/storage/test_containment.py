from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from document_processing.storage import IntegrityError, RunRepository


class RepositoryContainmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_initialize_rejects_root_and_runs_symlinks(self) -> None:
        for kind in ("root", "runs"):
            with self.subTest(kind=kind):
                case = self.base / kind
                outside = self.base / f"outside-{kind}"
                outside.mkdir()
                if kind == "root":
                    case.symlink_to(outside, target_is_directory=True)
                else:
                    case.mkdir()
                    (case / "runs").symlink_to(outside, target_is_directory=True)

                repository = RunRepository(case)
                with self.assertRaises(IntegrityError):
                    repository.initialize()

                self.assertFalse((outside / "state.sqlite3").exists())

    def test_create_run_rejects_a_run_directory_symlink(self) -> None:
        root = self.base / "artifacts"
        repository = RunRepository(root)
        repository.initialize()
        outside = self.base / "outside-run"
        outside.mkdir()
        (root / "runs" / "run-1").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(IntegrityError):
            repository.create_run("run-1", {"model": "test:model"})

        self.assertEqual(list(outside.iterdir()), [])

    def test_store_source_rejects_replaced_source_directory(self) -> None:
        root = self.base / "artifacts-source"
        repository = RunRepository(root)
        repository.initialize()
        repository.create_run("run-1", {"model": "test:model"})
        source_dir = root / "runs" / "run-1" / "source"
        source_dir.rmdir()
        outside = self.base / "outside-source"
        outside.mkdir()
        source_dir.symlink_to(outside, target_is_directory=True)
        upload = self.base / "upload.pdf"
        upload.write_bytes(b"%PDF-1.7\ncontained\n%%EOF\n")

        with self.assertRaises(IntegrityError):
            repository.store_source("run-1", upload)

        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
