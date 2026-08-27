from __future__ import annotations

import hashlib
from collections.abc import AsyncIterable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from document_processing.api.app import create_app
from document_processing.api.contracts import PagePage, ServiceError

RUN_ID = "00000000-0000-4000-8000-000000000001"


def run_data(
    *,
    run_id: str = RUN_ID,
    status: str = "not_started",
    failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "document_id": "a" * 64,
        "status": status,
        "phase": "queued" if status == "not_started" else "idle",
        "progress": {
            "total_pages": 1,
            "rendered_pages": 0,
            "completed_pages": 0,
            "current_page": None,
        },
        "failure": failure,
    }


class FakeRunService:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {RUN_ID: run_data()}
        self.pages: list[dict[str, Any]] = [{"page_number": 1, "status": "pending", "attempts": 0}]
        self.manifest: dict[str, Any] = {"schema_version": "1.0", "pages": []}
        self.upload: bytes | None = None
        self.submit_error: Exception | None = None
        self.ready_error: ServiceError | None = None
        self.ready_result: dict[str, Any] = {"ready": True, "reason": None}
        self.cancel_calls = 0

    async def submit_pdf(
        self,
        *,
        filename: str,
        content_type: str,
        chunks: AsyncIterable[bytes],
    ) -> dict[str, Any]:
        assert filename
        assert content_type in {"application/pdf", "application/x-pdf"}
        self.upload = b"".join([chunk async for chunk in chunks])
        if self.submit_error:
            raise self.submit_error
        created = run_data()
        created["document_id"] = hashlib.sha256(self.upload).hexdigest()
        self.runs[RUN_ID] = created
        return created

    async def get_run(self, run_id: str) -> dict[str, Any]:
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise ServiceError(
                "run_not_found",
                "No processing run has that identifier.",
                run_id=run_id,
            ) from exc

    async def list_pages(
        self,
        run_id: str,
        *,
        offset: int,
        limit: int,
    ) -> PagePage:
        await self.get_run(run_id)
        return PagePage(
            items=self.pages[offset : offset + limit],
            total=len(self.pages),
            offset=offset,
            limit=limit,
        )

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        run = await self.get_run(run_id)
        self.cancel_calls += 1
        if run["status"] == "completed":
            raise ServiceError(
                "invalid_run_state",
                "A completed run cannot be cancelled.",
                run_id=run_id,
            )
        run["status"] = "failed"
        run["phase"] = "idle"
        run["failure"] = {
            "code": "cancelled",
            "message": "Cancellation requested by the caller.",
            "retryable": True,
        }
        return run

    async def resume_run(self, run_id: str) -> dict[str, Any]:
        run = await self.get_run(run_id)
        if run["status"] == "completed":
            raise ServiceError(
                "invalid_run_state",
                "A completed run cannot be resumed.",
                run_id=run_id,
            )
        run["status"] = "resuming"
        run["phase"] = "queued"
        run["failure"] = None
        return run

    async def get_manifest(self, run_id: str) -> dict[str, Any]:
        await self.get_run(run_id)
        return self.manifest

    async def readiness(self) -> dict[str, Any]:
        if self.ready_error:
            raise self.ready_error
        return self.ready_result


@pytest.fixture
def service() -> FakeRunService:
    return FakeRunService()


@pytest.fixture
def client(service: FakeRunService) -> TestClient:
    return TestClient(create_app(service))
