"""ProcessingResult binds terminal outcomes to durable artifact identity."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from document_processing.contracts import RunStatus
from document_processing.processing import FailureCode, ProcessingFailure, ProcessingResult
from tests.processing.fakes import DOCUMENT_ID, FakeRepository, prepared_pdf


def _completed(root: Path) -> ProcessingResult:
    prepared = prepared_pdf(root, 1)
    repository = FakeRepository(root, prepared)
    return ProcessingResult(
        run_id="run-1",
        document_id=DOCUMENT_ID,
        status="completed",
        completed_pages=1,
        total_pages=1,
        artifact_root=repository.handle.run_directory,
        manifest_path=repository.handle.run_directory / "final/output_manifest.json",
        manifest=repository._manifest(),
    )


def test_completed_result_binds_manifest_and_stable_path(tmp_path: Path) -> None:
    result = _completed(tmp_path)

    assert result.manifest is not None
    assert result.document_id == result.manifest.document_id


@pytest.mark.parametrize("document_id", ["a" * 63, "A" * 64, "g" * 64])
def test_non_null_document_id_requires_lowercase_sha256(
    tmp_path: Path,
    document_id: str,
) -> None:
    failure = ProcessingFailure(FailureCode.STORAGE_FAILED, "failed")

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        ProcessingResult(
            run_id="run-1",
            document_id=document_id,
            status="failed",
            completed_pages=0,
            total_pages=1,
            artifact_root=tmp_path,
            failure=failure,
        )


def test_completed_result_rejects_wrong_manifest_path(tmp_path: Path) -> None:
    result = _completed(tmp_path)

    with pytest.raises(ValueError, match="stable final path"):
        replace(result, manifest_path=result.artifact_root / "output_manifest.json")


def test_completed_result_rejects_manifest_identity_mismatch(tmp_path: Path) -> None:
    result = _completed(tmp_path)

    with pytest.raises(ValueError, match="document identities"):
        replace(result, document_id="c" * 64)
    with pytest.raises(ValueError, match="run identities"):
        replace(result, run_id="another-run")


def test_completed_result_rejects_noncompleted_manifest(tmp_path: Path) -> None:
    result = _completed(tmp_path)
    assert result.manifest is not None
    noncompleted = result.manifest.model_copy(update={"status": RunStatus.RUNNING})

    with pytest.raises(ValueError, match="completed manifest"):
        replace(result, manifest=noncompleted)


def test_result_rejects_unknown_runtime_status(tmp_path: Path) -> None:
    failure = ProcessingFailure(FailureCode.UNEXPECTED, "failed")

    with pytest.raises(ValueError, match="status must be terminal"):
        ProcessingResult(
            run_id="run-1",
            document_id=None,
            status="running",  # type: ignore[arg-type]
            completed_pages=0,
            total_pages=0,
            artifact_root=tmp_path,
            failure=failure,
        )
