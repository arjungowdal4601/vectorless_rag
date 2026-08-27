"""Extract provider usage metadata without parsing model output content."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from langchain_core.messages import AIMessage

from document_processing.contracts import ModelUsageRecord

from .contracts import AnalyzerResultError


def collect_model_usage(
    *,
    messages: object,
    page_number: int,
    attempt_number: int,
    fallback_model_name: str,
    latency_ms: int,
) -> ModelUsageRecord:
    """Aggregate native LangChain usage fields from this isolated invocation."""

    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    provider_request_id: str | None = None
    model_id = fallback_model_name

    candidates = messages if isinstance(messages, Sequence) else ()
    for message in candidates:
        if not isinstance(message, AIMessage):
            continue
        metadata = message.response_metadata
        actual_model = _optional_text(metadata.get("model_name"))
        if actual_model is not None:
            model_id = actual_model
        request_id = _optional_text(metadata.get("id"))
        if request_id is None and message.id and message.id.startswith("resp_"):
            request_id = message.id
        if request_id is not None:
            provider_request_id = request_id

        usage = message.usage_metadata
        if usage is None:
            continue
        input_tokens += _token_count(usage, "input_tokens")
        output_tokens += _token_count(usage, "output_tokens")
        total_tokens += _token_count(usage, "total_tokens")

    if total_tokens == 0 and (input_tokens or output_tokens):
        total_tokens = input_tokens + output_tokens
    if total_tokens < input_tokens + output_tokens:
        raise AnalyzerResultError("Provider usage total is smaller than input plus output")

    return ModelUsageRecord(
        page_number=page_number,
        attempt_number=attempt_number,
        model_id=model_id,
        provider_request_id=provider_request_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
    )


def _token_count(usage: Mapping[str, object], key: str) -> int:
    value = usage.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AnalyzerResultError(f"Provider usage field {key!r} is invalid")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
