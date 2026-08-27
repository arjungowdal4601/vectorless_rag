"""Runtime gate for the pinned Deep Agents harness profile."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.exceptions import ContextOverflowError
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.base import LangSmithParams
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import Field

from document_processing.analysis import deep_agent

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _RecordingOpenAIModel(FakeListChatModel):
    """Offline model that records the tools bound after all middleware runs."""

    model_name: str = "gpt-5.6-luna"
    use_responses_api: bool = True
    bound_tool_names: list[tuple[str, ...]] = Field(default_factory=list)

    def _get_ls_params(
        self,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> LangSmithParams:
        del stop, kwargs
        return {
            "ls_provider": "openai",
            "ls_model_name": self.model_name,
            "ls_model_type": "chat",
        }

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        del tool_choice, kwargs
        names = tuple(_tool_name(tool) for tool in tools)
        self.bound_tool_names.append(names)
        return cast("Runnable[LanguageModelInput, AIMessage]", self)


class _OverflowThenResponseModel(_RecordingOpenAIModel):
    provider_calls: int = 0

    def _call(self, *args: Any, **kwargs: Any) -> str:
        self.provider_calls += 1
        if self.provider_calls == 1:
            raise ContextOverflowError("synthetic provider context overflow")
        return super()._call(*args, **kwargs)


class _ToolCallingOpenAIModel(FakeMessagesListChatModel):
    """Emit a forbidden harness tool call without ever exposing that tool."""

    model_name: str = "gpt-5.6-luna"
    bound_tool_names: list[tuple[str, ...]] = Field(default_factory=list)

    def _get_ls_params(
        self,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> LangSmithParams:
        del stop, kwargs
        return {
            "ls_provider": "openai",
            "ls_model_name": self.model_name,
            "ls_model_type": "chat",
        }

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        del tool_choice, kwargs
        self.bound_tool_names.append(tuple(_tool_name(tool) for tool in tools))
        return cast("Runnable[LanguageModelInput, AIMessage]", self)


def _tool_name(tool: dict[str, Any] | type | Callable[..., Any] | BaseTool) -> str:
    name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
    return name if isinstance(name, str) else "<unnamed>"


def _valid_response(edit_type: str = "append_new_section") -> str:
    return json.dumps(
        {
            "memory_edits": {
                "short_term_memory_edits": [
                    {
                        "edit_type": edit_type,
                        "section_heading": "Active Reading Position",
                        "content": {
                            "parent_section": None,
                            "current_subsection": None,
                            "last_visible_clause": None,
                            "current_topic_flow": None,
                            "unfinished_content": None,
                            "next_page_inspection": None,
                            "document_completion": "In progress",
                        },
                    }
                ],
                "document_structure_edits": {"topics": []},
            },
            "page_output": {
                "page_type": "blank",
                "index_decision": "non_index_worthy",
                "index_reason": "No substantive visible content.",
                "summary": "",
                "topics": [],
                "assets": [],
            },
        }
    )


def test_real_graph_exposes_no_tools_to_model_at_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real 0.7.9 graph and its final tool-exclusion middleware."""

    page_image = tmp_path / "page-0001.png"
    page_image.write_bytes(_ONE_PIXEL_PNG)
    model = _RecordingOpenAIModel(responses=[_valid_response()])

    def offline_chat_openai(**kwargs: object) -> _RecordingOpenAIModel:
        del kwargs
        return model

    # Only the provider transport is replaced. The production builder still
    # compiles and invokes deepagents.create_deep_agent with its real 0.7.9
    # filesystem, subagent, and tool-exclusion middleware stack.
    monkeypatch.setattr(deep_agent, "ChatOpenAI", offline_chat_openai)

    analyzer = deep_agent.build_deep_agent_analyzer()
    result = asyncio.run(
        analyzer.analyze(
            page_number=1,
            page_image_path=page_image,
            short_term_memory={},
        )
    )

    assert result.model_response.page_output.index_decision == "non_index_worthy"
    assert model.bound_tool_names == [()]


def test_context_overflow_is_one_application_owned_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harness must not hide a summarization retry inside one durable attempt."""

    page_image = tmp_path / "page-0001.png"
    page_image.write_bytes(_ONE_PIXEL_PNG)
    model = _OverflowThenResponseModel(responses=[_valid_response()])
    monkeypatch.setattr(deep_agent, "ChatOpenAI", lambda **_: model)
    analyzer = deep_agent.build_deep_agent_analyzer()

    with pytest.raises(ContextOverflowError):
        asyncio.run(
            analyzer.analyze(
                page_number=1,
                page_image_path=page_image,
                short_term_memory={},
            )
        )

    assert model.provider_calls == 1


def test_hostile_excluded_tool_call_cannot_execute_or_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_image = tmp_path / "page-0001.png"
    page_image.write_bytes(_ONE_PIXEL_PNG)
    model = _ToolCallingOpenAIModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ls",
                        "args": {"path": str(tmp_path)},
                        "id": "forbidden-ls",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=_valid_response()),
        ]
    )
    monkeypatch.setattr(deep_agent, "ChatOpenAI", lambda **_: model)
    analyzer = deep_agent.build_deep_agent_analyzer()

    with pytest.raises(ModelCallLimitExceededError):
        asyncio.run(
            analyzer.analyze(
                page_number=1,
                page_image_path=page_image,
                short_term_memory={},
            )
        )

    assert model.bound_tool_names == [()]
    assert model.i == 1


def test_excluded_write_tool_cannot_mutate_backend_or_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_image = tmp_path / "page-0001.png"
    page_image.write_bytes(_ONE_PIXEL_PNG)
    target = tmp_path / "forbidden-write.txt"
    model = _ToolCallingOpenAIModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"file_path": str(target), "content": "mutated"},
                        "id": "forbidden-write",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=_valid_response()),
        ]
    )
    monkeypatch.setattr(deep_agent, "ChatOpenAI", lambda **_: model)
    analyzer = deep_agent.build_deep_agent_analyzer()

    with pytest.raises(ModelCallLimitExceededError):
        asyncio.run(
            analyzer.analyze(
                page_number=1,
                page_image_path=page_image,
                short_term_memory={},
            )
        )

    assert model.bound_tool_names == [()]
    assert model.i == 1
    assert not target.exists()
