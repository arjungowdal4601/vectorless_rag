"""Fault-boundary reconciliation against authoritative repository state."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from PIL import Image

from document_processing.pdf import PdfRenderConfig, RenderManifest
from document_processing.processing import DocumentProcessor, FailureCode
from document_processing.runtime.processing_repository import DurableProcessingRepository
from document_processing.storage import PageCommitInput, RunRepository
from tests.pdf.helpers import PageSpec, make_pdf
from tests.processing.factories import analysis_result
from tests.processing.fakes import (
    FakeManifest,
    FakePdfIntake,
    FakePreparedPdf,
    FakeRenderPage,
    FakeRepository,
    QueueAnalyzer,
    prepared_pdf,
)


def _processor(
    root: Path,
) -> tuple[DocumentProcessor, FakeRepository]:
    prepared = prepared_pdf(root, 1)
    repository = FakeRepository(root, prepared)
    processor = DocumentProcessor(
        analyzer=QueueAnalyzer([analysis_result(1)]),
        pdf_intake=FakePdfIntake(prepared),
        repository=repository,
    )
    return processor, repository


def test_page_error_after_durable_commit_continues_without_second_attempt(
    tmp_path: Path,
) -> None:
    processor, repository = _processor(tmp_path)
    repository.commit_error_after_durable = OSError("injected after database commit")

    result = asyncio.run(processor.process_pdf(tmp_path / "input.pdf"))

    assert result.status == "completed"
    assert result.completed_pages == 1
    assert repository.attempt_counts == {1: 1}
    assert repository.failed_attempts == []


def test_page_error_before_durable_commit_fails_without_touching_reset_attempt(
    tmp_path: Path,
) -> None:
    processor, repository = _processor(tmp_path)
    repository.commit_error_before_durable = OSError("injected before database commit")

    result = asyncio.run(processor.process_pdf(tmp_path / "input.pdf"))

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is FailureCode.STORAGE_FAILED
    assert repository.failed_attempts == []
    assert repository.commits == []


def test_final_error_after_publication_returns_authoritative_completion(
    tmp_path: Path,
) -> None:
    processor, repository = _processor(tmp_path)
    repository.finalize_error_after_publish = OSError("injected after final database commit")

    result = asyncio.run(processor.process_pdf(tmp_path / "input.pdf"))

    assert result.status == "completed"
    assert result.manifest is not None
    assert repository.finalized
    assert repository.failures == []


class InjectedCrash(RuntimeError):
    pass


def _durable_processor(
    root: Path,
    *,
    failed_page: bool = False,
    cancelled: bool = False,
) -> tuple[DocumentProcessor, RunRepository]:
    config = {"model": "test:model", "schema_version": 1}
    storage = RunRepository(root / "artifacts")
    storage.initialize()
    storage.create_run("run-1", config)
    upload = root / "upload.pdf"
    make_pdf(upload, (PageSpec(),))
    storage.store_source("run-1", upload)
    paths = storage.paths_for_run("run-1")
    image = paths.page_images_dir / "page-0001.png"
    Image.new("RGB", (200, 100), (1, 2, 3)).save(image, format="PNG")
    source_hash = str(storage.get_run("run-1")["source_sha256"])
    image_hash = hashlib.sha256(image.read_bytes()).hexdigest()
    storage.set_render_manifest(
        "run-1",
        RenderManifest.build(
            schema_version=1,
            renderer="pypdfium2",
            source_path="source/original.pdf",
            document_id=source_hash,
            source_size_bytes=paths.source_pdf.stat().st_size,
            configuration_fingerprint=PdfRenderConfig().fingerprint,
            dpi=200,
            color_mode="RGB",
            image_format="PNG",
            page_count=1,
            pages=(
                {
                    "page_number": 1,
                    "image_path": "page_images/page-0001.png",
                    "image_sha256": image_hash,
                    "width_px": 200,
                    "height_px": 100,
                    "image_size_bytes": image.stat().st_size,
                },
            ),
        ),
    )
    storage.initialize_artifacts("run-1", {}, {"pages": []})
    storage.mark_run_running("run-1")
    if failed_page:
        attempt_id = storage.begin_page_attempt("run-1", 1)
        storage.fail_page_attempt(
            "run-1",
            attempt_id,
            code="provider_failure",
            detail="test page failure",
            failure_class="permanent_model",
        )
    else:
        if cancelled:
            storage.request_cancel("run-1")
        storage.mark_run_failed(
            "run-1",
            "cancelled" if cancelled else "interrupted",
            "test resume setup",
            failure_class="cancelled" if cancelled else "transient_model",
        )
    prepared = FakePreparedPdf(
        document_id=source_hash,
        source_path=paths.source_pdf,
        manifest_path=paths.render_manifest,
        manifest=FakeManifest(
            1,
            (FakeRenderPage(1, "page_images/page-0001.png"),),
        ),
        page_image_paths=(image,),
    )
    fingerprint = str(storage.get_run("run-1")["config_sha256"])
    adapter = DurableProcessingRepository(storage, config, fingerprint)
    processor = DocumentProcessor(
        analyzer=QueueAnalyzer([analysis_result(1, attempt=2 if failed_page else 1)]),
        pdf_intake=FakePdfIntake(prepared),
        repository=adapter,
    )
    return processor, storage


def test_real_page_after_database_commit_is_reconciled(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    processor, storage = _durable_processor(tmp_path)
    original = storage.commit_page

    def commit_with_crash(value: PageCommitInput) -> object:
        def crash(step: str) -> None:
            if step == "after_db_commit":
                raise InjectedCrash(step)

        return original(replace(value, fault_hook=crash))

    monkeypatch.setattr(storage, "commit_page", commit_with_crash)
    result = asyncio.run(processor.resume("run-1"))

    assert result.status == "completed"
    assert storage.get_pages("run-1")[0]["attempt_count"] == 1
    assert storage.get_run("run-1")["status"] == "completed"


def test_real_final_after_database_commit_is_reconciled(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    processor, storage = _durable_processor(tmp_path)
    original = storage.finalize_run

    def finalize_with_crash(
        final: Any,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> Mapping[str, Any]:
        def crash(step: str) -> None:
            if step == "after_final_db_commit":
                raise InjectedCrash(step)

        return original(final, cancel_check=cancel_check, fault_hook=crash)

    monkeypatch.setattr(storage, "finalize_run", finalize_with_crash)
    result = asyncio.run(processor.resume("run-1"))

    assert result.status == "completed"
    assert storage.get_run("run-1")["status"] == "completed"


def test_direct_resume_requeues_real_failed_page(tmp_path: Path) -> None:
    processor, storage = _durable_processor(tmp_path, failed_page=True)

    result = asyncio.run(processor.resume("run-1"))

    assert result.status == "completed"
    assert storage.get_pages("run-1")[0]["attempt_count"] == 2


def test_direct_resume_atomically_clears_real_cancel(tmp_path: Path) -> None:
    processor, storage = _durable_processor(tmp_path, cancelled=True)

    result = asyncio.run(processor.resume("run-1"))

    assert result.status == "completed"
    assert storage.get_run("run-1")["cancel_requested"] == 0
