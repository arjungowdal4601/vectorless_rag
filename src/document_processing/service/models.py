"""Typed DTOs at the local service boundary."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from document_processing.contracts import PageStatus, RunStatus


class DtoMapping(Mapping[str, Any]):
    """Small immutable DTO that also satisfies the API's mapping protocol."""

    _mapping_fields: ClassVar[tuple[str, ...]] = ()

    def __getitem__(self, key: str) -> Any:
        if key not in self._mapping_fields:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._mapping_fields)

    def __len__(self) -> int:
        return len(self._mapping_fields)


TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED})


@dataclass(frozen=True, slots=True)
class FailureView(DtoMapping):
    _mapping_fields = ("code", "category", "message", "retryable")
    code: str
    category: str
    message: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class ProgressView(DtoMapping):
    _mapping_fields = (
        "completed_pages",
        "total_pages",
        "rendered_pages",
        "current_page",
    )
    completed_pages: int = 0
    total_pages: int | None = None
    rendered_pages: int = 0
    current_page: int | None = None


@dataclass(frozen=True, slots=True)
class RunView(DtoMapping):
    _mapping_fields = (
        "run_id",
        "document_id",
        "status",
        "phase",
        "progress",
        "resumable",
        "cancel_requested",
        "created_at",
        "updated_at",
        "failure",
    )
    run_id: str
    document_id: str
    status: RunStatus
    phase: str
    progress: ProgressView
    resumable: bool
    cancel_requested: bool
    created_at: datetime
    updated_at: datetime
    failure: FailureView | None = None


@dataclass(frozen=True, slots=True)
class PageView(DtoMapping):
    _mapping_fields = (
        "page_number",
        "status",
        "page_image_path",
        "page_json_path",
        "attempt_count",
        "failure",
    )
    page_number: int
    status: PageStatus
    page_image_path: str | None = None
    page_json_path: str | None = None
    attempt_count: int = 0
    failure: FailureView | None = None


@dataclass(frozen=True, slots=True)
class PagePage:
    """Pagination DTO kept as a dataclass because ``Mapping.items`` conflicts."""

    items: Sequence[PageView]
    offset: int
    limit: int
    total: int


@dataclass(frozen=True, slots=True)
class HealthView(DtoMapping):
    _mapping_fields = ("status",)
    status: str = "ok"


@dataclass(frozen=True, slots=True)
class ReadyView(DtoMapping):
    _mapping_fields = ("ready", "reason")
    ready: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Submission:
    run_id: str
    document_id: str
    filename: str
    content_type: str
    size_bytes: int
    source_path: Path
    configuration_fingerprint: str
    configuration: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RecoveryItem:
    run_id: str
    source_path: Path
    resume: bool = True


@dataclass(frozen=True, slots=True)
class WorkItem:
    run_id: str
    source_path: Path
    resume: bool = False


@dataclass(frozen=True, slots=True)
class ProcessingOutcome:
    """Optional result from a processor; the repository remains authoritative."""

    run_id: str
    status: RunStatus
    metadata: Mapping[str, Any] = field(default_factory=dict)
