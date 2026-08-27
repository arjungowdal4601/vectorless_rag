"""Privacy and context-isolation gates for real Deep Agents invocations."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.base import LangSmithParams
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import Runnable
from langchain_core.runnables.config import (
    RunnableConfig,
    var_child_runnable_config,
)
from langchain_core.tools import BaseTool
from langchain_core.tracers.context import collect_runs, tracing_v2_enabled
from pydantic import Field

from document_processing.analysis import deep_agent

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _MessageRecordingModel(FakeListChatModel):
    model_name: str = "gpt-5.6-luna"
    use_responses_api: bool = True
    seen_messages: list[list[BaseMessage]] = Field(default_factory=list)

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
        del tools, tool_choice, kwargs
        return cast("Runnable[LanguageModelInput, AIMessage]", self)

    def _call(
        self,
        messages: list[BaseMessage],
        *args: Any,
        **kwargs: Any,
    ) -> str:
        self.seen_messages.append(list(messages))
        return super()._call(messages, *args, **kwargs)


class _AmbientCapture(BaseCallbackHandler):
    """Callback that records any nested input made visible to its caller."""

    def __init__(self) -> None:
        self.payloads: list[str] = []

    def on_chain_start(self, serialized: object, inputs: object, **_: Any) -> None:
        self.payloads.append(repr((serialized, inputs)))

    def on_chat_model_start(
        self,
        serialized: object,
        messages: object,
        **_: Any,
    ) -> None:
        self.payloads.append(repr((serialized, messages)))


class _NoSendLangSmithClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def create_run(self, *_: object, **__: object) -> None:
        self.calls.append("create")

    def update_run(self, *_: object, **__: object) -> None:
        self.calls.append("update")


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


def _analyze_page(
    analyzer: deep_agent.DeepAgentAnalyzer,
    image: Path,
    *,
    page_number: int = 1,
    memory: dict[str, object] | None = None,
) -> object:
    return asyncio.run(
        analyzer.analyze(
            page_number=page_number,
            page_image_path=image,
            short_term_memory=memory or {},
        )
    )


def test_ambient_tracing_and_callbacks_cannot_observe_page_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_image = tmp_path / "page-0001.png"
    page_image.write_bytes(_ONE_PIXEL_PNG)
    encoded_image = base64.b64encode(_ONE_PIXEL_PNG).decode("ascii")
    model = _MessageRecordingModel(responses=[_valid_response()])
    monkeypatch.setattr(deep_agent, "ChatOpenAI", lambda **_: model)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    analyzer = deep_agent.build_deep_agent_analyzer()
    capture = _AmbientCapture()
    client = _NoSendLangSmithClient()
    config_token = var_child_runnable_config.set(RunnableConfig(callbacks=[capture]))
    try:
        with (
            tracing_v2_enabled(client=cast(Any, client)) as tracer,
            collect_runs() as collector,
        ):
            _analyze_page(analyzer, page_image)
    finally:
        var_child_runnable_config.reset(config_token)

    assert all(encoded_image not in payload for payload in capture.payloads)
    assert capture.payloads == []
    assert collector.traced_runs == []
    assert tracer.latest_run is None
    assert client.calls == []


def test_two_real_graph_calls_do_not_share_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_image = tmp_path / "page-0001.png"
    second_image = tmp_path / "page-0002.png"
    first_image.write_bytes(_ONE_PIXEL_PNG)
    second_image.write_bytes(_ONE_PIXEL_PNG + b"page-two-marker")
    model = _MessageRecordingModel(
        responses=[_valid_response(), _valid_response("replace_section")]
    )
    monkeypatch.setattr(deep_agent, "ChatOpenAI", lambda **_: model)
    analyzer = deep_agent.build_deep_agent_analyzer()

    _analyze_page(analyzer, first_image)
    _analyze_page(
        analyzer,
        second_image,
        page_number=2,
        memory={"call_marker": "only-second-call"},
    )

    assert len(model.seen_messages) == 2
    first_payload = repr(model.seen_messages[0])
    second_payload = repr(model.seen_messages[1])
    first_encoded = base64.b64encode(_ONE_PIXEL_PNG).decode("ascii")
    second_encoded = base64.b64encode(_ONE_PIXEL_PNG + b"page-two-marker").decode("ascii")
    assert first_encoded in first_payload
    assert second_encoded not in first_payload
    assert "only-second-call" not in first_payload
    assert second_encoded in second_payload
    assert first_encoded not in second_payload
    assert "only-second-call" in second_payload
