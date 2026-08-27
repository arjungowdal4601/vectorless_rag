"""ASGI lifespan composition for the local run service."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol


class ManagedService(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...


class LifespanRouter(Protocol):
    lifespan_context: Callable[[Any], AbstractAsyncContextManager[Any]]


class LifespanApplication(Protocol):
    router: LifespanRouter


def install_service_lifespan(app: LifespanApplication, service: ManagedService) -> None:
    """Compose service ownership around any lifespan installed by the API."""

    application_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: Any) -> AsyncIterator[Any]:
        await service.start()
        try:
            async with application_lifespan(application) as state:
                yield state
        finally:
            await service.close()

    app.router.lifespan_context = lifespan
