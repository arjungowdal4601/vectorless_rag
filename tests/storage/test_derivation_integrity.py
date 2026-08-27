from __future__ import annotations

import json
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from document_processing.storage import PageCommitInput
from document_processing.storage.files import canonical_json, sha256_bytes

from .helpers import (
    commit_input,
    make_repository,
    second_commit_input,
)


class ChangingDump:
    def __init__(self, first: Any, later: Any) -> None:
        self.first = first
        self.later = later
        self.calls = 0

    def model_dump(self, **_kwargs: Any) -> Any:
        self.calls += 1
        return deepcopy(self.first if self.calls == 1 else self.later)


class DerivationIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_commit_rejects_individually_valid_contradictory_artifacts(self) -> None:
        cases = (
            ("page", "page artifact", self._contradict_page),
            ("memory", "short-term memory", self._contradict_memory),
            ("structure", "document structure", self._contradict_structure),
        )
        for name, message, mutate in cases:
            with self.subTest(artifact=name):
                repository = make_repository(self.root / name)
                value = mutate(commit_input(repository))

                with self.assertRaisesRegex(ValueError, message):
                    repository.commit_page(value)

                self.assertEqual(repository.get_run("run-1")["head_page"], 0)
                self.assertFalse(
                    repository.paths_for_run("run-1")
                    .page_artifacts_dir.joinpath("page-0001")
                    .exists()
                )

    def test_commit_rejects_rewritten_prior_structure_prefix(self) -> None:
        repository = make_repository(self.root, page_count=2)
        repository.commit_page(commit_input(repository))
        value = second_commit_input(repository)
        structure = deepcopy(value.document_structure)
        structure["pages"][0]["topics"] = ["Rewritten prior topic"]

        with self.assertRaisesRegex(ValueError, "prior prefix"):
            repository.commit_page(replace(value, document_structure=structure))

        self.assertEqual(repository.get_run("run-1")["head_page"], 1)
        self.assertFalse(
            repository.paths_for_run("run-1").page_artifacts_dir.joinpath("page-0002").exists()
        )

    def test_commit_persists_the_validated_immutable_snapshot(self) -> None:
        repository = make_repository(self.root)
        value = commit_input(repository)
        memory = ChangingDump(value.short_term_memory, {})

        repository.commit_page(replace(value, short_term_memory=memory))

        stored = (
            repository.paths_for_run("run-1").page_artifacts_dir
            / "page-0001"
            / "short_term_memory.json"
        )
        self.assertEqual(json.loads(stored.read_text(encoding="utf-8")), value.short_term_memory)
        self.assertEqual(memory.calls, 1)
        self.assertTrue(repository.audit_run("run-1").ok)

    def test_audit_replays_structure_prefix_after_hash_consistent_tampering(self) -> None:
        repository = make_repository(self.root, page_count=2)
        repository.commit_page(commit_input(repository))
        repository.commit_page(second_commit_input(repository))
        directory = repository.paths_for_run("run-1").page_artifacts_dir / "page-0002"
        structure_path = directory / "document_structure.json"
        structure = json.loads(structure_path.read_text(encoding="utf-8"))
        structure["pages"][0]["topics"] = ["Rewritten prior topic"]
        structure_bytes = canonical_json(structure)
        structure_path.write_bytes(structure_bytes)
        structure_hash = sha256_bytes(structure_bytes)

        manifest_path = directory / "commit.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["document_structure.json"] = structure_hash
        manifest_bytes = canonical_json(manifest)
        manifest_path.write_bytes(manifest_bytes)
        commit_id = sha256_bytes(manifest_bytes)
        with repository._db.immediate() as connection:
            connection.execute(
                """UPDATE commits SET commit_id=?,manifest_sha256=?,structure_sha256=?
                   WHERE run_id='run-1' AND page_number=2""",
                (commit_id, commit_id, structure_hash),
            )
            connection.execute(
                "UPDATE pages SET commit_id=? WHERE run_id='run-1' AND page_number=2",
                (commit_id,),
            )
            connection.execute(
                "UPDATE runs SET head_commit_id=? WHERE run_id='run-1'",
                (commit_id,),
            )

        report = repository.audit_run("run-1")

        self.assertFalse(report.ok)
        self.assertTrue(
            any(
                issue.code == "commit_invalid" and "prior prefix" in issue.message
                for issue in report.issues
            )
        )
        recovered = repository.recover_run("run-1")
        self.assertFalse(recovered.audit.ok)
        self.assertEqual(repository.get_run("run-1")["failure_class"], "integrity")

    @staticmethod
    def _contradict_page(value: PageCommitInput) -> PageCommitInput:
        page = deepcopy(value.page)
        page["summary"] = "A valid but contradictory page summary."
        return replace(value, page=page)

    @staticmethod
    def _contradict_memory(value: PageCommitInput) -> PageCommitInput:
        memory = deepcopy(value.short_term_memory)
        memory["Active Reading Position"]["current_subsection"] = "Contradiction"
        return replace(value, short_term_memory=memory)

    @staticmethod
    def _contradict_structure(value: PageCommitInput) -> PageCommitInput:
        structure = deepcopy(value.document_structure)
        structure["pages"][0]["topics"] = ["Contradiction"]
        return replace(value, document_structure=structure)


if __name__ == "__main__":
    unittest.main()
