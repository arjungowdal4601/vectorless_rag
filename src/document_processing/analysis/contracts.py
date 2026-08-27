"""Public analyzer boundary used by the processing orchestrator."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable

from document_processing.contracts import ModelResponse, ModelUsageRecord


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """One provider-validated page response and its model usage."""

    model_response: ModelResponse
    usage: ModelUsageRecord


@runtime_checkable
class Analyzer(Protocol):
    """Analyze one page without retaining context between calls."""

    async def analyze(
        self,
        *,
        page_number: int,
        page_image_path: Path,
        short_term_memory: Mapping[str, object],
        attempt_number: int = 1,
    ) -> AnalysisResult:
        """Analyze the current page using only its image, number, and memory."""

        ...


class AnalyzerError(Exception):
    """Permanent analyzer boundary failure.

    Provider exceptions intentionally propagate unchanged so the outer processor
    can classify transient transport and service failures.
    """

    retryable: ClassVar[bool] = False


class AnalyzerInputError(AnalyzerError):
    """The deterministic analyzer input is invalid."""


class AnalyzerResultError(AnalyzerError):
    """The graph did not return its provider-validated structured response."""
