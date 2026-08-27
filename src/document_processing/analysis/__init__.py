"""Stateless multimodal page-analysis boundary."""

from .contracts import (
    AnalysisResult,
    Analyzer,
    AnalyzerError,
    AnalyzerInputError,
    AnalyzerResultError,
)
from .deep_agent import (
    MODEL_NAME,
    MODEL_REASONING_EFFORT,
    MODEL_TIMEOUT_SECONDS,
    DeepAgentAnalyzer,
    ReasoningEffort,
    build_deep_agent_analyzer,
    get_default_analyzer,
)
from .fake import AnalysisCall, FakeAnalyzer, FakeOutcome

__all__ = [
    "MODEL_NAME",
    "MODEL_REASONING_EFFORT",
    "MODEL_TIMEOUT_SECONDS",
    "AnalysisCall",
    "AnalysisResult",
    "Analyzer",
    "AnalyzerError",
    "AnalyzerInputError",
    "AnalyzerResultError",
    "DeepAgentAnalyzer",
    "FakeAnalyzer",
    "FakeOutcome",
    "ReasoningEffort",
    "build_deep_agent_analyzer",
    "get_default_analyzer",
]
