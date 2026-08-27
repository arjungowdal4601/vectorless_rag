"""Offline end-to-end tests across rendering, orchestration, and persistence."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from document_processing.analysis import AnalysisResult, FakeAnalyzer
from document_processing.api import create_app
from document_processing.composition import create_processor, create_service
from document_processing.config import Settings
from document_processing.contracts import ProcessingManifest
from document_processing.storage import RunRepository
from tests.pdf.helpers import PageSpec, make_pdf
from tests.processing.factories import analysis_result


class GateAnalyzer:
    """Pause the first provider response so cancellation can win the race."""

    def __init__(self, outcomes: list[AnalysisResult]) -> None:
        self.outcomes = outcomes
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.attempt_numbers: list[int] = []

    async def analyze(
        self,
        *,
        page_number: int,
        page_image_path: Path,
        short_term_memory: dict[str, object],
        attempt_number: int = 1,
    ) -> AnalysisResult:
        del page_number, page_image_path, short_term_memory
        self.attempt_numbers.append(attempt_number)
        self.started.set()
        await self.release.wait()
        return self.outcomes.pop(0)


def test_real_processor_publishes_only_fully_audited_output(tmp_path: Path) -> None:
    source = tmp_path / "two-pages.pdf"
    source_bytes = make_pdf(
        source,
        (PageSpec(72, 36), PageSpec(90, 45, rotation=90)),
    )
    settings = Settings(artifact_root=tmp_path / "artifacts")
    storage = RunRepository(settings.artifact_root)
    analyzer = FakeAnalyzer([analysis_result(1), analysis_result(2)])
    processor = create_processor(settings, analyzer=analyzer, storage=storage)

    result = asyncio.run(processor.process_pdf(source))

    assert result.status == "completed", result.failure
    assert result.manifest is not None
    assert result.manifest_path is not None
    assert result.manifest_path.is_file()
    assert result.completed_pages == result.total_pages == 2
    assert result.document_id == hashlib.sha256(source_bytes).hexdigest()
    assert [call.page_number for call in analyzer.calls] == [1, 2]
    assert analyzer.calls[0].short_term_memory == {}
    assert list(analyzer.calls[1].short_term_memory) == ["Active Reading Position"]

    run_root = settings.artifact_root / "runs" / result.run_id
    assert (run_root / "source/original.pdf").read_bytes() == source_bytes
    assert storage.audit_run(result.run_id).require_ok().head_page == 2
    final_names = {path.name for path in (run_root / "final").iterdir()}
    assert final_names == {
        "output_manifest.json",
        "short_term_memory.json",
        "document_structure.json",
        "model_usage.json",
        "processing_status.json",
        "recovery_events.json",
    }
    restored = ProcessingManifest.model_validate_json(result.manifest_path.read_bytes())
    assert restored == result.manifest
    structure = json.loads((run_root / "final/document_structure.json").read_text())
    assert structure == {
        "pages": [
            {"page_number": 1, "topics": ["Section 1"]},
            {"page_number": 2, "topics": ["Section 2"]},
        ]
    }


def test_composed_http_service_runs_one_pdf_to_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "service.pdf"
    source_bytes = make_pdf(source, (PageSpec(),))
    settings = Settings(artifact_root=tmp_path / "service-artifacts")
    analyzer = FakeAnalyzer([analysis_result(1)])
    monkeypatch.setattr(
        "document_processing.composition.build_deep_agent_analyzer",
        lambda **_kwargs: analyzer,
    )
    service = create_service(settings)
    app = create_app(service)

    async def scenario() -> None:
        await service.start()
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                accepted = await client.post(
                    "/v1/runs",
                    files={"file": ("service.pdf", source_bytes, "application/pdf")},
                )
                assert accepted.status_code == 202
                run_id = accepted.json()["run_id"]
                finished = await service.wait(run_id, timeout=5)
                assert finished.status.value == "completed"

                status = await client.get(f"/v1/runs/{run_id}")
                pages = await client.get(f"/v1/runs/{run_id}/pages")
                manifest = await client.get(f"/v1/runs/{run_id}/manifest")

                assert status.status_code == 200
                assert status.json()["progress"] == {
                    "completed_pages": 1,
                    "total_pages": 1,
                    "rendered_pages": 1,
                    "current_page": None,
                }
                assert pages.status_code == 200
                assert pages.json()["items"][0]["status"] == "completed"
                assert manifest.status_code == 200
                assert manifest.json()["status"] == "completed"
        finally:
            await service.close()

    asyncio.run(scenario())


def test_cancelled_response_is_discarded_and_resume_uses_durable_ordinal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "cancel-resume.pdf"
    make_pdf(source, (PageSpec(),))
    settings = Settings(artifact_root=tmp_path / "cancel-artifacts")
    analyzer = GateAnalyzer([analysis_result(1), analysis_result(1, attempt=2)])
    monkeypatch.setattr(
        "document_processing.composition.build_deep_agent_analyzer",
        lambda **_kwargs: analyzer,
    )
    service = create_service(settings)

    async def scenario() -> None:
        await service.start()
        try:
            accepted = await service.submit_pdf(source)
            await asyncio.wait_for(analyzer.started.wait(), timeout=5)
            requested = await service.cancel_run(accepted.run_id)
            assert requested.cancel_requested
            analyzer.release.set()

            cancelled = await service.wait(accepted.run_id, timeout=5)
            assert cancelled.status.value == "failed"
            assert cancelled.failure is not None
            assert cancelled.failure.code == "cancelled"
            pending = await service.list_pages(accepted.run_id)
            assert pending.items[0].status.value == "pending"
            assert pending.items[0].page_json_path is None

            resumed = await service.resume_run(accepted.run_id)
            assert resumed.status.value == "resuming"
            completed = await service.wait(accepted.run_id, timeout=5)
            assert completed.status.value == "completed"
            assert analyzer.attempt_numbers == [1, 2]
        finally:
            await service.close()

    asyncio.run(scenario())


def test_startup_quarantines_unregistered_run_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(artifact_root=tmp_path / "orphan-artifacts")
    orphan = settings.artifact_root / "runs" / "unregistered"
    (orphan / "source").mkdir(parents=True)
    (orphan / "source/original.pdf").write_bytes(b"%PDF-orphan")
    monkeypatch.setattr(
        "document_processing.composition.build_deep_agent_analyzer",
        lambda **_kwargs: FakeAnalyzer([]),
    )
    service = create_service(settings)

    async def scenario() -> None:
        await service.start()
        try:
            assert (await service.readiness()).ready
            assert not orphan.exists()
            quarantined = list((settings.artifact_root / "quarantine").iterdir())
            assert len(quarantined) == 1
            assert (quarantined[0] / "source/original.pdf").is_file()
        finally:
            await service.close()

    asyncio.run(scenario())


def test_startup_audits_failed_run_and_reclassifies_source_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "failed.pdf"
    make_pdf(source, (PageSpec(),))
    settings = Settings(artifact_root=tmp_path / "failed-artifacts")
    storage = RunRepository(settings.artifact_root)
    processor = create_processor(
        settings,
        analyzer=FakeAnalyzer([ValueError("permanent provider failure")]),
        storage=storage,
    )
    failed = asyncio.run(processor.process_pdf(source))
    assert failed.status == "failed"
    (failed.artifact_root / "source/original.pdf").unlink()
    monkeypatch.setattr(
        "document_processing.composition.build_deep_agent_analyzer",
        lambda **_kwargs: FakeAnalyzer([]),
    )
    service = create_service(settings)

    async def scenario() -> None:
        await service.start()
        try:
            view = await service.get_run(failed.run_id)
            assert view.status.value == "failed"
            assert view.failure is not None
            assert view.failure.category == "integrity"
            assert not view.resumable
        finally:
            await service.close()

    asyncio.run(scenario())


def test_startup_claims_and_processes_accepted_not_started_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "accepted.pdf"
    make_pdf(source, (PageSpec(),))
    settings = Settings(artifact_root=tmp_path / "accepted-artifacts")
    storage = RunRepository(settings.artifact_root)
    storage.initialize()
    run_id = str(uuid4())
    storage.create_run(run_id, settings.fingerprint_payload())
    storage.store_source(run_id, source, original_filename=source.name)
    monkeypatch.setattr(
        "document_processing.composition.build_deep_agent_analyzer",
        lambda **_kwargs: FakeAnalyzer([analysis_result(1)]),
    )
    service = create_service(settings)

    async def scenario() -> None:
        await service.start()
        try:
            completed = await service.wait(run_id, timeout=5)
            assert completed.status.value == "completed"
        finally:
            await service.close()

    asyncio.run(scenario())


def test_startup_claims_and_resumes_interrupted_running_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "interrupted.pdf"
    make_pdf(source, (PageSpec(),))
    settings = Settings(
        artifact_root=tmp_path / "interrupted-artifacts",
        shutdown_grace_seconds=0,
    )
    interrupted_analyzer = GateAnalyzer([analysis_result(1)])
    analyzers = iter((interrupted_analyzer, FakeAnalyzer([analysis_result(1, attempt=2)])))
    monkeypatch.setattr(
        "document_processing.composition.build_deep_agent_analyzer",
        lambda **_kwargs: next(analyzers),
    )

    async def scenario() -> None:
        first = create_service(settings)
        await first.start()
        accepted = await first.submit_pdf(source)
        await asyncio.wait_for(interrupted_analyzer.started.wait(), timeout=5)
        await first.close()
        assert RunRepository(settings.artifact_root).get_run(accepted.run_id)["status"] == "running"

        restarted = create_service(settings)
        await restarted.start()
        try:
            completed = await restarted.wait(accepted.run_id, timeout=5)
            assert completed.status.value == "completed"
        finally:
            await restarted.close()

    asyncio.run(scenario())
