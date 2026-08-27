"""Deterministic analyzer fake for processor and service tests."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from .contracts import AnalysisResult


@dataclass(frozen=True, slots=True)
class AnalysisCall:
    """Captured arguments from one fake analyzer invocation."""

    page_number: int
    page_image_path: Path
    short_term_memory: Mapping[str, object]
    attempt_number: int


type FakeOutcome = AnalysisResult | Exception


class FakeAnalyzer:
    """Return queued results or exceptions while recording every call."""

    def __init__(self, outcomes: Iterable[FakeOutcome]) -> None:
        self._outcomes = deque(outcomes)
        self.calls: list[AnalysisCall] = []

    async def analyze(
        self,
        *,
        page_number: int,
        page_image_path: Path,
        short_term_memory: Mapping[str, object],
        attempt_number: int = 1,
    ) -> AnalysisResult:
        self.calls.append(
            AnalysisCall(
                page_number=page_number,
                page_image_path=page_image_path,
                short_term_memory=deepcopy(dict(short_term_memory)),
                attempt_number=attempt_number,
            )
        )
        if not self._outcomes:
            raise AssertionError("FakeAnalyzer has no queued outcome")
        outcome = self._outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
