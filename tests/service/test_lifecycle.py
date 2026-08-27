"""ASGI lifespan ownership tests."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from document_processing.service.lifecycle import install_service_lifespan


class ServiceSpy:
    def __init__(self) -> None:
        self.started = 0
        self.closed = 0

    async def start(self) -> None:
        self.started += 1

    async def close(self) -> None:
        self.closed += 1


def test_installed_lifespan_owns_service_once() -> None:
    app = FastAPI()
    service = ServiceSpy()
    install_service_lifespan(app, service)  # type: ignore[arg-type]

    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 200
        assert service.started == 1
        assert service.closed == 0

    assert service.closed == 1
