from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from document_processing.api.app import create_app
from document_processing.api.contracts import ServiceError

RUN_ID = "00000000-0000-4000-8000-000000000001"


def test_submit_pdf_returns_accepted_links(
    client: TestClient,
    service: Any,
) -> None:
    pdf = b"%PDF-1.7\nminimal-test-pdf"

    response = client.post(
        "/v1/runs",
        files={"file": ("sample.pdf", pdf, "application/pdf")},
    )

    assert response.status_code == 202
    assert service.upload == pdf
    body = response.json()
    assert body == {
        "run_id": RUN_ID,
        "document_id": body["document_id"],
        "status": "not_started",
        "status_url": f"/v1/runs/{RUN_ID}",
        "pages_url": f"/v1/runs/{RUN_ID}/pages",
        "manifest_url": f"/v1/runs/{RUN_ID}/manifest",
    }
    assert len(body["document_id"]) == 64


def test_submit_requires_exactly_one_file_part(client: TestClient) -> None:
    response = client.post(
        "/v1/runs",
        files={"file": ("sample.pdf", b"%PDF", "application/pdf")},
        data={"label": "extra-part"},
    )

    assert_problem(response, 400, "invalid_request")


def test_submit_requires_named_file_part(client: TestClient) -> None:
    response = client.post(
        "/v1/runs",
        files={"pdf": ("sample.pdf", b"%PDF", "application/pdf")},
    )

    assert_problem(response, 400, "invalid_request")


def test_submit_rejects_non_multipart_request(client: TestClient) -> None:
    response = client.post(
        "/v1/runs",
        content=b"%PDF",
        headers={"content-type": "application/pdf"},
    )

    assert_problem(response, 415, "unsupported_media_type")


def test_submit_rejects_non_pdf_file_media_type(client: TestClient) -> None:
    response = client.post(
        "/v1/runs",
        files={"file": ("sample.txt", b"text", "text/plain")},
    )

    assert_problem(response, 415, "unsupported_media_type")


def test_pdf_media_type_is_case_insensitive_and_allows_parameters(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/runs",
        files={"file": ("sample.pdf", b"%PDF", "Application/PDF; version=1.7")},
    )

    assert response.status_code == 202


def test_service_pdf_validation_error_is_problem(
    client: TestClient,
    service: Any,
) -> None:
    service.submit_error = ServiceError("invalid_pdf", "The PDF cannot be parsed.")

    response = client.post(
        "/v1/runs",
        files={"file": ("bad.pdf", b"not-pdf", "application/pdf")},
    )

    assert_problem(response, 422, "invalid_pdf")


def test_get_run_and_missing_run(client: TestClient) -> None:
    assert client.get(f"/v1/runs/{RUN_ID}").status_code == 200

    response = client.get("/v1/runs/missing")

    assert_problem(response, 404, "run_not_found")
    assert response.json()["run_id"] == "missing"


def test_forbidden_run_status_is_never_exposed(
    client: TestClient,
    service: Any,
) -> None:
    service.runs[RUN_ID]["status"] = "cancelled"

    response = client.get(f"/v1/runs/{RUN_ID}")

    assert_problem(response, 500, "internal_error")


def test_invalid_internal_run_and_document_ids_are_never_exposed(
    client: TestClient,
    service: Any,
) -> None:
    for field, value in (("run_id", "run-1"), ("document_id", "not-a-sha256")):
        original = service.runs[RUN_ID][field]
        service.runs[RUN_ID][field] = value
        response = client.get(f"/v1/runs/{RUN_ID}")
        service.runs[RUN_ID][field] = original
        assert_problem(response, 500, "internal_error")


def test_pages_are_paginated(
    client: TestClient,
    service: Any,
) -> None:
    service.pages = [
        {"page_number": number, "status": "pending", "attempts": 0} for number in range(1, 5)
    ]

    response = client.get(f"/v1/runs/{RUN_ID}/pages?offset=1&limit=2")

    assert response.status_code == 200
    assert response.json() == {
        "items": [service.pages[1], service.pages[2]],
        "total": 4,
        "offset": 1,
        "limit": 2,
    }


def test_page_pagination_validation_uses_problem_json(client: TestClient) -> None:
    response = client.get(f"/v1/runs/{RUN_ID}/pages?offset=-1&limit=501")

    assert_problem(response, 422, "validation_error")


def test_page_pagination_uses_service_configuration(service: Any) -> None:
    service.settings = type(
        "ConfiguredSettings",
        (),
        {
            "max_upload_bytes": 1024,
            "page_list_default_limit": 2,
            "page_list_max_limit": 3,
        },
    )()
    service.pages = [
        {"page_number": number, "status": "pending", "attempts": 0} for number in range(1, 6)
    ]
    configured = TestClient(create_app(service))

    response = configured.get(f"/v1/runs/{RUN_ID}/pages")
    too_large = configured.get(f"/v1/runs/{RUN_ID}/pages?limit=4")

    assert response.status_code == 200
    assert response.json()["limit"] == 2
    assert len(response.json()["items"]) == 2
    assert_problem(too_large, 422, "validation_error")


def test_cancel_is_idempotent_and_uses_failed_status(
    client: TestClient,
    service: Any,
) -> None:
    first = client.post(f"/v1/runs/{RUN_ID}/cancel")
    second = client.post(f"/v1/runs/{RUN_ID}/cancel")

    assert first.status_code == second.status_code == 202
    assert first.json()["status"] == second.json()["status"] == "failed"
    assert first.json()["failure"]["code"] == "cancelled"
    assert "cancelled" not in {first.json()["status"], second.json()["status"]}
    assert service.cancel_calls == 2


def test_resume_returns_resuming(client: TestClient) -> None:
    client.post(f"/v1/runs/{RUN_ID}/cancel")

    response = client.post(f"/v1/runs/{RUN_ID}/resume")

    assert response.status_code == 202
    assert response.json()["status"] == "resuming"
    assert response.json()["failure"] is None


def test_manifest_is_conflict_until_completed(
    client: TestClient,
    service: Any,
) -> None:
    pending = client.get(f"/v1/runs/{RUN_ID}/manifest")
    assert_problem(pending, 409, "result_not_ready")

    service.runs[RUN_ID]["status"] = "completed"
    service.runs[RUN_ID]["phase"] = "idle"
    complete = client.get(f"/v1/runs/{RUN_ID}/manifest")

    assert complete.status_code == 200
    assert complete.json() == service.manifest


def assert_problem(response: Any, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == status
    assert body["code"] == code
    assert body["type"].endswith(f":{code}")
    assert body["instance"].startswith("/")
