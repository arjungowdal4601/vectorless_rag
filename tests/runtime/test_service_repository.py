"""Lifecycle consistency tests for the concrete async service adapter."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from document_processing.errors import InvalidRunOperationError
from document_processing.runtime import DurableRunServiceRepository
from document_processing.storage import RunRepository
from tests.storage.helpers import make_repository


def test_configuration_mismatch_is_not_advertised_as_resumable(tmp_path: Path) -> None:
    storage = make_repository(tmp_path)
    storage.mark_run_failed(
        "run-1",
        "model_permanent",
        "Page analysis failed.",
        failure_class="permanent_model",
    )
    adapter = DurableRunServiceRepository(storage, "f" * 64, 3)

    view = asyncio.run(adapter.get_run("run-1"))

    assert not view.resumable
    with pytest.raises(InvalidRunOperationError):
        asyncio.run(adapter.prepare_resume("run-1"))


def test_failed_pending_run_is_atomically_claimed_for_resume(tmp_path: Path) -> None:
    storage = make_repository(tmp_path)
    storage.mark_run_failed("run-1", "model_permanent", "Failed.", failure_class="permanent_model")
    fingerprint = str(storage.get_run("run-1")["config_sha256"])
    adapter = DurableRunServiceRepository(storage, fingerprint, 3)

    view, source = asyncio.run(adapter.prepare_resume("run-1"))

    assert view.status.value == "resuming"
    assert storage.get_run("run-1")["status"] == "resuming"
    assert storage.get_page("run-1", 1)["status"] == "pending"
    assert source == storage.paths_for_run("run-1").source_pdf


def test_failed_pre_render_run_can_be_claimed_for_resume(tmp_path: Path) -> None:
    storage = RunRepository(tmp_path / "artifacts")
    storage.initialize()
    created = storage.create_run("pre-render", {"model": "test"})
    upload = tmp_path / "source.pdf"
    upload.write_bytes(b"%PDF-1.7\nfixture\n%%EOF\n")
    storage.store_source("pre-render", upload)
    storage.mark_run_failed("pre-render", "rendering_failed", "Failed.", failure_class="rendering")
    adapter = DurableRunServiceRepository(storage, str(created["config_sha256"]), 3)

    view, source = asyncio.run(adapter.prepare_resume("pre-render"))

    assert view.status.value == "resuming"
    assert source.read_bytes() == upload.read_bytes()


def test_skipped_page_is_not_advertised_or_accepted_for_resume(tmp_path: Path) -> None:
    storage = make_repository(tmp_path)
    storage.mark_run_failed(
        "run-1",
        "reserved_skip",
        "Reserved state.",
        failure_class="permanent_model",
    )
    with sqlite3.connect(storage.root / "state.sqlite3") as connection:
        connection.execute(
            "UPDATE pages SET status='skipped' WHERE run_id='run-1' AND page_number=1"
        )
    fingerprint = str(storage.get_run("run-1")["config_sha256"])
    adapter = DurableRunServiceRepository(storage, fingerprint, 3)

    view = asyncio.run(adapter.get_run("run-1"))

    assert not view.resumable
    with pytest.raises(InvalidRunOperationError):
        asyncio.run(adapter.prepare_resume("run-1"))
    assert storage.get_run("run-1")["status"] == "failed"


def test_exhausted_tampered_run_is_audited_before_resume_rejection(
    tmp_path: Path,
) -> None:
    storage = make_repository(tmp_path)
    for ordinal in range(1, 4):
        attempt = storage.begin_page_attempt("run-1", 1)
        storage.fail_page_attempt(
            "run-1",
            attempt,
            code="temporary_failure",
            detail="Temporary model failure.",
            failure_class="transient_model",
            will_retry=ordinal < 3,
        )
        if ordinal < 3:
            storage.mark_run_running("run-1")
    storage.paths_for_run("run-1").source_pdf.unlink()
    fingerprint = str(storage.get_run("run-1")["config_sha256"])
    adapter = DurableRunServiceRepository(storage, fingerprint, 3)

    with pytest.raises(InvalidRunOperationError):
        asyncio.run(adapter.prepare_resume("run-1"))

    row = storage.get_run("run-1")
    assert row["status"] == "failed"
    assert row["failure_class"] == "integrity"
    assert not asyncio.run(adapter.get_run("run-1")).resumable


def test_repeated_cancelled_request_is_idempotent(tmp_path: Path) -> None:
    storage = make_repository(tmp_path)
    storage.mark_run_failed("run-1", "cancelled", "Cancelled.", failure_class="cancelled")
    fingerprint = str(storage.get_run("run-1")["config_sha256"])
    adapter = DurableRunServiceRepository(storage, fingerprint, 3)

    view = asyncio.run(adapter.request_cancel("run-1"))

    assert view.status.value == "failed"
    assert view.failure is not None
    assert view.failure.code == "cancelled"


@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_cancel_returns_unrelated_terminal_run_unchanged(tmp_path: Path, terminal: str) -> None:
    storage = make_repository(tmp_path)
    if terminal == "completed":
        with sqlite3.connect(storage.root / "state.sqlite3") as connection:
            connection.execute(
                "UPDATE runs SET status='completed', phase='completed' WHERE run_id='run-1'"
            )
    else:
        storage.mark_run_failed(
            "run-1", "model_permanent", "Failed.", failure_class="permanent_model"
        )
    fingerprint = str(storage.get_run("run-1")["config_sha256"])
    adapter = DurableRunServiceRepository(storage, fingerprint, 3)

    before = storage.get_run("run-1")
    view = asyncio.run(adapter.request_cancel("run-1"))

    assert view.status.value == terminal
    assert storage.get_run("run-1")["failure_code"] == before["failure_code"]


def test_cancel_uses_atomic_returned_row_when_failure_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = make_repository(tmp_path)
    fingerprint = str(storage.get_run("run-1")["config_sha256"])
    original = storage.request_cancel

    def failure_wins(run_id: str) -> object:
        storage.mark_run_failed(
            run_id, "model_permanent", "Failed.", failure_class="permanent_model"
        )
        return original(run_id)

    monkeypatch.setattr(storage, "request_cancel", failure_wins)
    adapter = DurableRunServiceRepository(storage, fingerprint, 3)

    view = asyncio.run(adapter.request_cancel("run-1"))

    assert view.status.value == "failed"
    assert storage.get_run("run-1")["failure_code"] == "model_permanent"
