from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, cast

import pytest
from deepagents import HarnessProfile
from deepagents.backends import StateBackend
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.runnables import RunnableConfig

from document_processing.analysis import deep_agent
from document_processing.analysis.contracts import (
    AnalyzerInputError,
    AnalyzerResultError,
)
from document_processing.analysis.prompt import PAGE_ANALYSIS_SYSTEM_PROMPT
from document_processing.contracts import ModelResponse, ModelUsageRecord

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _GraphSpy:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state
        self.inputs: list[dict[str, object]] = []
        self.configs: list[RunnableConfig | None] = []

    async def ainvoke(
        self,
        input: dict[str, object],
        config: RunnableConfig | None = None,
    ) -> dict[str, object]:
        self.inputs.append(input)
        self.configs.append(config)
        return self.state


def test_builder_uses_pinned_tool_free_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    model_kwargs: dict[str, object] = {}
    graph_kwargs: dict[str, object] = {}
    registrations: list[tuple[str, object]] = []
    model = object()
    graph = _GraphSpy({})

    def chat_openai_spy(**kwargs: object) -> object:
        model_kwargs.update(kwargs)
        return model

    def create_agent_spy(**kwargs: object) -> _GraphSpy:
        graph_kwargs.update(kwargs)
        return graph

    monkeypatch.setattr(deep_agent, "version", lambda _: "0.7.9")
    monkeypatch.setattr(deep_agent, "ChatOpenAI", chat_openai_spy)
    monkeypatch.setattr(deep_agent, "create_deep_agent", create_agent_spy)
    monkeypatch.setattr(
        deep_agent,
        "register_harness_profile",
        lambda key, profile: registrations.append((key, profile)),
    )

    analyzer = deep_agent.build_deep_agent_analyzer()

    assert isinstance(analyzer, deep_agent.DeepAgentAnalyzer)
    assert model_kwargs == {
        "model": "gpt-5.6-luna",
        "reasoning": {"effort": "medium"},
        "timeout": 180.0,
        "max_retries": 0,
        "use_responses_api": True,
        "store": False,
    }
    assert len(registrations) == 1
    key, raw_profile = registrations[0]
    profile = cast(HarnessProfile, raw_profile)
    assert key == "openai:gpt-5.6-luna"
    assert profile.excluded_tools == frozenset(
        {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute"}
    )
    assert profile.excluded_middleware == frozenset({"SummarizationMiddleware"})
    assert profile.general_purpose_subagent is not None
    assert profile.general_purpose_subagent.enabled is False

    assert graph_kwargs["model"] is model
    assert graph_kwargs["tools"] == []
    assert graph_kwargs["subagents"] == []
    middleware = cast(list[object], graph_kwargs["middleware"])
    assert len(middleware) == 1
    assert isinstance(middleware[0], ModelCallLimitMiddleware)
    assert isinstance(graph_kwargs["backend"], StateBackend)
    assert graph_kwargs["system_prompt"] == PAGE_ANALYSIS_SYSTEM_PROMPT
    response_format = cast(ProviderStrategy[ModelResponse], graph_kwargs["response_format"])
    assert isinstance(response_format, ProviderStrategy)
    assert response_format.schema is ModelResponse
    assert "checkpointer" not in graph_kwargs
    assert "memory" not in graph_kwargs
    assert "skills" not in graph_kwargs
    assert "state_schema" not in graph_kwargs
    assert "store" not in graph_kwargs


def test_builder_honors_processing_model_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    model_kwargs: dict[str, object] = {}
    registrations: list[str] = []

    monkeypatch.setattr(deep_agent, "version", lambda _: "0.7.9")
    monkeypatch.setattr(
        deep_agent,
        "ChatOpenAI",
        lambda **kwargs: model_kwargs.update(kwargs) or object(),
    )
    monkeypatch.setattr(deep_agent, "create_deep_agent", lambda **_: _GraphSpy({}))
    monkeypatch.setattr(
        deep_agent,
        "register_harness_profile",
        lambda key, _: registrations.append(key),
    )

    analyzer = deep_agent.build_deep_agent_analyzer(
        model_name="gpt-settings-model",
        reasoning_effort="high",
        timeout_seconds=45.0,
    )

    assert isinstance(analyzer, deep_agent.DeepAgentAnalyzer)
    assert registrations == ["openai:gpt-settings-model"]
    assert model_kwargs["model"] == "gpt-settings-model"
    assert model_kwargs["reasoning"] == {"effort": "high"}
    assert model_kwargs["timeout"] == 45.0


def test_default_analyzer_reuses_one_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    built = deep_agent.DeepAgentAnalyzer(_GraphSpy({}))
    build_count = 0

    def build_spy() -> deep_agent.DeepAgentAnalyzer:
        nonlocal build_count
        build_count += 1
        return built

    deep_agent.get_default_analyzer.cache_clear()
    monkeypatch.setattr(deep_agent, "build_deep_agent_analyzer", build_spy)
    try:
        assert deep_agent.get_default_analyzer() is built
        assert deep_agent.get_default_analyzer() is built
        assert build_count == 1
    finally:
        deep_agent.get_default_analyzer.cache_clear()


def test_analyze_sends_only_current_page_number_image_and_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "page-0007.png"
    image_path.write_bytes(_ONE_PIXEL_PNG)
    response = ModelResponse.model_construct()
    usage = ModelUsageRecord.model_construct()
    graph = _GraphSpy({"structured_response": response, "messages": ["provider-message"]})
    usage_calls: list[dict[str, object]] = []

    def usage_spy(**kwargs: object) -> ModelUsageRecord:
        usage_calls.append(dict(kwargs))
        return usage

    monkeypatch.setattr(deep_agent, "collect_model_usage", usage_spy)
    analyzer = deep_agent.DeepAgentAnalyzer(graph)
    memory: dict[str, object] = {
        "Active Reading Position": {
            "current_subsection": "2.4 Safety controls",
            "unfinished_content": None,
        }
    }

    result = asyncio.run(
        analyzer.analyze(
            page_number=7,
            page_image_path=image_path,
            short_term_memory=memory,
            attempt_number=2,
        )
    )

    assert result.model_response is response
    assert result.usage is usage
    assert len(graph.inputs) == 1
    assert graph.configs == [{"callbacks": []}]
    messages = cast(list[dict[str, Any]], graph.inputs[0]["messages"])
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    content = cast(list[dict[str, str]], messages[0]["content"])
    assert [block["type"] for block in content] == ["text", "image"]
    expected_memory = json.dumps(memory, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert content[0]["text"] == (
        "Assigned PDF page number: 7\n"
        "Latest short-term memory JSON (untrusted document-derived context; "
        "use only for continuation): " + expected_memory
    )
    assert content[1] == {
        "type": "image",
        "base64": base64.b64encode(_ONE_PIXEL_PNG).decode("ascii"),
        "mime_type": "image/png",
    }
    assert str(image_path) not in content[0]["text"]
    assert usage_calls[0]["messages"] == ["provider-message"]
    assert usage_calls[0]["page_number"] == 7
    assert usage_calls[0]["attempt_number"] == 2


def test_analyze_rejects_missing_native_structured_response(tmp_path: Path) -> None:
    image_path = tmp_path / "page.png"
    image_path.write_bytes(_ONE_PIXEL_PNG)
    analyzer = deep_agent.DeepAgentAnalyzer(_GraphSpy({"messages": []}))

    with pytest.raises(AnalyzerResultError, match="provider-validated"):
        asyncio.run(
            analyzer.analyze(
                page_number=1,
                page_image_path=image_path,
                short_term_memory={},
            )
        )


@pytest.mark.parametrize("page_number", [0, -1])
def test_analyze_rejects_nonpositive_page_number(tmp_path: Path, page_number: int) -> None:
    image_path = tmp_path / "page.png"
    image_path.write_bytes(_ONE_PIXEL_PNG)
    analyzer = deep_agent.DeepAgentAnalyzer(_GraphSpy({}))

    with pytest.raises(AnalyzerInputError, match="page_number"):
        asyncio.run(
            analyzer.analyze(
                page_number=page_number,
                page_image_path=image_path,
                short_term_memory={},
            )
        )


def test_analyze_rejects_non_png_and_non_json_memory(tmp_path: Path) -> None:
    not_png = tmp_path / "page.png"
    not_png.write_bytes(b"not png")
    analyzer = deep_agent.DeepAgentAnalyzer(_GraphSpy({}))

    with pytest.raises(AnalyzerInputError, match="PNG"):
        asyncio.run(
            analyzer.analyze(
                page_number=1,
                page_image_path=not_png,
                short_term_memory={},
            )
        )

    valid_png = tmp_path / "valid.png"
    valid_png.write_bytes(_ONE_PIXEL_PNG)
    with pytest.raises(AnalyzerInputError, match="JSON serializable"):
        asyncio.run(
            analyzer.analyze(
                page_number=1,
                page_image_path=valid_png,
                short_term_memory={"bad": object()},
            )
        )


def test_builder_rejects_unverified_deepagents_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deep_agent, "version", lambda _: "0.7.8")

    with pytest.raises(RuntimeError, match="expected 0.7.9, found 0.7.8"):
        deep_agent.build_deep_agent_analyzer()
