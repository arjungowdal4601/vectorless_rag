from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from document_processing.api.app import create_app
from document_processing.api.body_limit import MULTIPART_OVERHEAD_BYTES
from document_processing.api.contracts import ServiceError
from document_processing.errors import QueueCapacityError


def test_health_and_readiness(
    client: TestClient,
    service: Any,
) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"ready": True, "reason": None}

    service.ready_error = ServiceError(
        "worker_unavailable",
        "The embedded worker is not running.",
    )
    unavailable = client.get("/readyz")

    assert_problem(unavailable, 503, "worker_unavailable")


def test_false_readiness_result_is_503_problem(
    client: TestClient,
    service: Any,
) -> None:
    service.ready_result = {"ready": False, "reason": "storage_unavailable"}

    response = client.get("/readyz")

    assert_problem(response, 503, "storage_unavailable")


def test_unknown_route_uses_problem_json(client: TestClient) -> None:
    assert_problem(client.get("/does-not-exist"), 404, "not_found")


def test_method_not_allowed_uses_problem_json(client: TestClient) -> None:
    assert_problem(client.delete("/healthz"), 405, "method_not_allowed")


def test_queue_full_has_retry_after(
    client: TestClient,
    service: Any,
) -> None:
    service.submit_error = QueueCapacityError("The local processing queue is full.")

    response = client.post(
        "/v1/runs",
        files={"file": ("sample.pdf", b"%PDF", "application/pdf")},
    )

    assert_problem(response, 429, "queue_full")
    assert response.headers["retry-after"] == "1"


def test_unexpected_error_is_sanitized() -> None:
    class ExplodingService:
        async def readiness(self) -> dict[str, str]:
            raise RuntimeError("secret implementation detail")

    client = TestClient(create_app(ExplodingService()), raise_server_exceptions=False)  # type: ignore[arg-type]

    response = client.get("/readyz")

    assert_problem(response, 500, "internal_error")
    assert "secret implementation detail" not in response.text


def test_declared_oversize_upload_is_rejected_before_form_parsing(service: Any) -> None:
    client = TestClient(create_app(service, max_upload_bytes=8))

    response = client.post(
        "/v1/runs",
        headers={
            "content-type": "multipart/form-data; boundary=test",
            "content-length": str(9 + MULTIPART_OVERHEAD_BYTES),
        },
        content=b"ignored",
    )

    assert_problem(response, 413, "upload_too_large")
    assert service.upload is None


def test_streamed_oversize_upload_is_bounded_without_content_length(
    service: Any,
) -> None:
    client = TestClient(create_app(service, max_upload_bytes=8))
    boundary = "bounded-upload"
    header = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="large.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode()
    footer = f"\r\n--{boundary}--\r\n".encode()

    def body() -> Any:
        yield header
        yield b"%PDF" + b"x" * MULTIPART_OVERHEAD_BYTES
        yield footer

    response = client.post(
        "/v1/runs",
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        content=body(),
    )

    assert_problem(response, 413, "upload_too_large")
    assert service.upload is None


def assert_problem(response: Any, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == status
    assert body["code"] == code
    assert body["type"].endswith(f":{code}")
    assert body["instance"].startswith("/")
