"""Run the loopback-only FastAPI application."""

from __future__ import annotations

from importlib import import_module
from typing import cast

from fastapi import FastAPI

from document_processing.config import Settings
from document_processing.service.lifecycle import LifespanApplication, install_service_lifespan


def create_application(settings: Settings | None = None) -> FastAPI:
    """Compose concrete adapters lazily so imports remain side-effect free."""

    from document_processing.api.app import create_app

    configured = settings or Settings.from_env()
    composition = import_module("document_processing.composition")
    service = composition.create_service(configured)
    app = create_app(service)
    install_service_lifespan(cast(LifespanApplication, app), service)
    return app


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run(
        create_application(settings),
        host=settings.host,
        port=settings.port,
        access_log=True,
        log_level="info",
    )


if __name__ == "__main__":
    main()
