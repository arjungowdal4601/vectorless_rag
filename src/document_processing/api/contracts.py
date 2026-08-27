"""Structural contract between the HTTP layer and the local run service."""

from __future__ import annotations

from collections.abc import AsyncIterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

JsonObject = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PagePage:
    """One stable, bounded page of per-PDF-page processing states."""

    items: Sequence[JsonObject]
    total: int
    offset: int
    limit: int


class ServiceError(Exception):
    """Expected service-boundary failure that is safe to expose via HTTP."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        run_id: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.run_id = run_id


@runtime_checkable
class LocalRunServiceProtocol(Protocol):
    """Operations needed by the API; implementations own persistence and work."""

    async def submit_pdf(
        self,
        *,
        filename: str,
        content_type: str,
        chunks: AsyncIterable[bytes],
    ) -> object: ...

    async def get_run(self, run_id: str) -> object: ...

    async def list_pages(
        self,
        run_id: str,
        *,
        offset: int,
        limit: int,
    ) -> object: ...

    async def cancel_run(self, run_id: str) -> object: ...

    async def resume_run(self, run_id: str) -> object: ...

    async def get_manifest(self, run_id: str) -> object: ...

    async def readiness(self) -> object: ...
