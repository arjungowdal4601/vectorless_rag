from __future__ import annotations

import hashlib
import sqlite3
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread, current_thread
from unittest.mock import patch

from document_processing.pdf import PdfIntakeService, PdfRenderConfig
from document_processing.pdf.models import RenderManifest
from document_processing.storage import CancelledBeforeCommit, IntegrityError
from tests.pdf.helpers import PageSpec, make_pdf

from .helpers import commit_input, completed_manifest, make_repository


class AuditAndFinalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_tampered_snapshot_fails_closed(self) -> None:
        repository = make_repository(self.root)
        repository.commit_page(commit_input(repository))
        memory = (
            repository.paths_for_run("run-1").page_artifacts_dir
            / "page-0001"
            / "short_term_memory.json"
        )
        memory.write_text("{}\n", encoding="utf-8")

        audit = repository.audit_run("run-1")
        self.assertFalse(audit.ok)
        self.assertTrue(any(issue.code == "commit_invalid" for issue in audit.issues))
        repository.recover_run("run-1")
        run = repository.get_run("run-1")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failure_class"], "integrity")

    def test_render_manifest_is_not_enriched_or_invalidated(self) -> None:
        repository = make_repository(self.root, run_id="baseline")
        repository.create_run("strict-render", {"model": "test:model"})
        upload = self.root / "strict.pdf"
        paths = repository.paths_for_run("strict-render")
        make_pdf(upload, (PageSpec(),))
        prepared = PdfIntakeService(PdfRenderConfig()).prepare_path(upload, paths.root)
        repository.store_source("strict-render", prepared.source_path)
        strict = prepared.manifest

        repository.set_render_manifest("strict-render", strict)

        restored = RenderManifest.model_validate_json(paths.render_manifest.read_bytes())
        self.assertEqual(restored, strict)
        self.assertNotIn("source_sha256", restored.model_dump(mode="json"))

    def test_run_fingerprint_uses_compact_json_without_newline(self) -> None:
        repository = make_repository(self.root, run_id="fingerprint")
        expected = hashlib.sha256(b'{"model":"test:model","schema_version":1}').hexdigest()

        self.assertEqual(repository.get_run("fingerprint")["config_sha256"], expected)

    def test_run_fingerprint_matches_settings_utf8_encoding(self) -> None:
        repository = make_repository(self.root, run_id="existing")
        repository.create_run("unicode", {"model": "模型"})
        expected = hashlib.sha256('{"model":"模型"}'.encode()).hexdigest()

        self.assertEqual(repository.get_run("unicode")["config_sha256"], expected)

    def test_run_manifest_tampering_is_detected(self) -> None:
        repository = make_repository(self.root)
        repository.paths_for_run("run-1").run_manifest.write_text("{}\n", encoding="utf-8")

        report = repository.audit_run("run-1")

        self.assertFalse(report.ok)
        self.assertTrue(any(issue.code == "run_manifest_invalid" for issue in report.issues))

    def test_unreferenced_page_images_are_quarantined_before_rerender(self) -> None:
        repository = make_repository(self.root, run_id="existing")
        repository.create_run("render-crash", {"model": "test:model"})
        repository.mark_run_running("render-crash")
        upload = self.root / "render-crash.pdf"
        upload.write_bytes(b"%PDF-1.7\nrender crash\n%%EOF\n")
        repository.store_source("render-crash", upload)
        paths = repository.paths_for_run("render-crash")
        (paths.page_images_dir / "page-0001.png").write_bytes(b"orphan image")

        recovered = repository.recover_run("render-crash")

        self.assertTrue(recovered.audit.ok)
        self.assertEqual(list(paths.page_images_dir.iterdir()), [])
        self.assertTrue(recovered.quarantined)
        self.assertEqual(repository.get_run("render-crash")["phase"], "rendering")

    def test_unjournaled_published_render_bundle_is_quarantined(self) -> None:
        repository = make_repository(self.root, run_id="existing")
        repository.create_run("publish-crash", {"model": "test:model"})
        repository.mark_run_running("publish-crash")
        upload = self.root / "publish-crash.pdf"
        upload.write_bytes(b"%PDF-1.7\npublish crash\n%%EOF\n")
        repository.store_source("publish-crash", upload)
        paths = repository.paths_for_run("publish-crash")
        (paths.page_images_dir / "page-0001.png").write_bytes(b"published image")
        paths.render_manifest.write_text('{"published":true}\n', encoding="utf-8")

        recovered = repository.recover_run("publish-crash")

        self.assertTrue(recovered.audit.ok)
        self.assertFalse(paths.render_manifest.exists())
        self.assertEqual(list(paths.page_images_dir.iterdir()), [])
        self.assertEqual(len(recovered.quarantined), 1)
        quarantined = paths.root / recovered.quarantined[0]
        self.assertEqual(
            {path.name for path in quarantined.iterdir()},
            {"render_manifest.json", "page_images"},
        )
        self.assertTrue((quarantined / "page_images" / "page-0001.png").is_file())
        self.assertEqual(repository.get_run("publish-crash")["phase"], "rendering")

    def test_referenced_render_manifest_corruption_fails_closed(self) -> None:
        repository = make_repository(self.root)
        repository.paths_for_run("run-1").render_manifest.unlink()

        report = repository.recover_run("run-1")

        self.assertFalse(report.audit.ok)
        run = repository.get_run("run-1")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failure_class"], "integrity")

    def test_audit_rejects_unmanifested_or_unsafe_page_image_entries(self) -> None:
        for kind in ("file", "directory", "symlink"):
            with self.subTest(kind=kind):
                repository = make_repository(self.root / kind)
                image_dir = repository.paths_for_run("run-1").page_images_dir
                extra = image_dir / "unmanifested"
                if kind == "file":
                    extra.write_bytes(b"unmanifested image")
                elif kind == "directory":
                    extra.mkdir()
                else:
                    extra.symlink_to(image_dir / "page-0001.png")

                report = repository.audit_run("run-1")

                self.assertFalse(report.ok)
                self.assertTrue(
                    any(issue.code == "render_manifest_invalid" for issue in report.issues)
                )

    def test_finalization_writes_only_approved_outputs(self) -> None:
        repository = make_repository(self.root)
        repository.commit_page(commit_input(repository))
        final = repository.prepare_final_manifest("run-1", completed_manifest(repository))
        result = repository.finalize_run(final)

        self.assertEqual(result["status"], "completed")
        final_dir = repository.paths_for_run("run-1").final_dir
        self.assertEqual(
            {path.name for path in final_dir.iterdir()},
            {
                "output_manifest.json",
                "short_term_memory.json",
                "document_structure.json",
                "model_usage.json",
                "processing_status.json",
                "recovery_events.json",
            },
        )
        self.assertTrue(repository.audit_run("run-1").ok)

    def test_final_candidate_audit_blocks_corrupt_terminal_publication(self) -> None:
        repository = make_repository(self.root)
        repository.commit_page(commit_input(repository))
        final = repository.prepare_final_manifest("run-1", completed_manifest(repository))
        usage = repository.paths_for_run("run-1").final_dir / "model_usage.json"
        usage.write_text("[]\n", encoding="utf-8")

        with self.assertRaises(IntegrityError):
            repository.finalize_run(final)

        self.assertEqual(repository.get_run("run-1")["status"], "running")

    def test_completed_run_audit_detects_final_projection_tampering(self) -> None:
        repository = make_repository(self.root)
        repository.commit_page(commit_input(repository))
        final = repository.prepare_final_manifest("run-1", completed_manifest(repository))
        repository.finalize_run(final)
        usage = repository.paths_for_run("run-1").final_dir / "model_usage.json"
        usage.write_text("[]\n", encoding="utf-8")

        report = repository.audit_run("run-1")
        self.assertFalse(report.ok)
        self.assertTrue(any(issue.code == "final_output_invalid" for issue in report.issues))
        demoted = repository.mark_completed_integrity_failed("run-1")
        self.assertEqual(demoted["status"], "failed")
        self.assertEqual(demoted["failure_class"], "integrity")

    def test_in_memory_cancel_check_blocks_final_publication(self) -> None:
        repository = make_repository(self.root)
        repository.commit_page(commit_input(repository))
        final = repository.prepare_final_manifest("run-1", completed_manifest(repository))

        with self.assertRaises(CancelledBeforeCommit):
            repository.finalize_run(final, cancel_check=lambda: True)

        run = repository.get_run("run-1")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failure_code"], "cancelled")
        self.assertFalse(repository.paths_for_run("run-1").final_dir.exists())

    def test_cancellation_racing_finalization_cannot_demote_completed_run(self) -> None:
        repository = make_repository(self.root)
        repository.commit_page(commit_input(repository))
        final = repository.prepare_final_manifest("run-1", completed_manifest(repository))
        finalizing = Event()
        cancel_started = Event()
        release_finalization = Event()
        failures: list[BaseException] = []

        def final_hook(step: str) -> None:
            if step == "before_final_db_commit":
                finalizing.set()
                if not release_finalization.wait(timeout=5):
                    raise TimeoutError("cancellation did not enter the race")

        def finalize() -> None:
            try:
                repository.finalize_run(final, fault_hook=final_hook)
            except BaseException as error:
                failures.append(error)

        def cancel() -> None:
            cancel_started.set()
            try:
                repository.request_cancel("run-1")
            except BaseException as error:
                failures.append(error)

        final_thread = Thread(target=finalize)
        final_thread.start()
        self.assertTrue(finalizing.wait(timeout=5))
        cancel_thread = Thread(target=cancel)
        cancel_thread.start()
        self.assertTrue(cancel_started.wait(timeout=5))
        release_finalization.set()
        final_thread.join(timeout=5)
        cancel_thread.join(timeout=5)

        self.assertFalse(final_thread.is_alive())
        self.assertFalse(cancel_thread.is_alive())
        self.assertEqual(failures, [])
        repository.mark_run_failed("run-1", "late_failure", "must be ignored")
        run = repository.get_run("run-1")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["cancel_requested"], 0)
        self.assertIsNone(run["failure_code"])

    def test_recovery_racing_finalization_cannot_demote_completed_run(self) -> None:
        repository = make_repository(self.root)
        repository.commit_page(commit_input(repository))
        final = repository.prepare_final_manifest("run-1", completed_manifest(repository))
        finalizing = Event()
        recovery_entered_transaction = Event()
        release_finalization = Event()
        failures: list[BaseException] = []
        original_immediate = repository._db.immediate

        @contextmanager
        def tracked_immediate() -> Iterator[sqlite3.Connection]:
            if current_thread().name == "recovery-race":
                recovery_entered_transaction.set()
            with original_immediate() as connection:
                yield connection

        def final_hook(step: str) -> None:
            if step == "before_final_db_commit":
                finalizing.set()
                if not release_finalization.wait(timeout=5):
                    raise TimeoutError("recovery did not enter the race")

        def finalize() -> None:
            try:
                repository.finalize_run(final, fault_hook=final_hook)
            except BaseException as error:
                failures.append(error)

        def recover() -> None:
            try:
                repository.recover_run("run-1")
            except BaseException as error:
                failures.append(error)

        with patch.object(repository._db, "immediate", tracked_immediate):
            final_thread = Thread(target=finalize)
            final_thread.start()
            self.assertTrue(finalizing.wait(timeout=5))
            recovery_thread = Thread(target=recover, name="recovery-race")
            recovery_thread.start()
            self.assertTrue(recovery_entered_transaction.wait(timeout=5))
            release_finalization.set()
            final_thread.join(timeout=5)
            recovery_thread.join(timeout=5)

        self.assertFalse(final_thread.is_alive())
        self.assertFalse(recovery_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(repository.get_run("run-1")["status"], "completed")
        self.assertTrue(repository.audit_run("run-1").ok)


if __name__ == "__main__":
    unittest.main()
