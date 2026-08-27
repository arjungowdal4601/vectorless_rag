from langchain_core.messages import AIMessage, HumanMessage

from document_processing.analysis.usage import collect_model_usage


def test_collect_model_usage_uses_native_ai_message_metadata() -> None:
    messages = [
        HumanMessage("page context"),
        AIMessage(
            content="",
            id="resp_fallback",
            response_metadata={
                "id": "resp_provider_123",
                "model_name": "gpt-5.6-luna-2026-08-20",
            },
            usage_metadata={
                "input_tokens": 120,
                "output_tokens": 45,
                "total_tokens": 165,
            },
        ),
    ]

    usage = collect_model_usage(
        messages=messages,
        page_number=4,
        attempt_number=2,
        fallback_model_name="gpt-5.6-luna",
        latency_ms=321,
    )

    assert usage.model_dump() == {
        "page_number": 4,
        "attempt_number": 2,
        "model_id": "gpt-5.6-luna-2026-08-20",
        "provider_request_id": "resp_provider_123",
        "input_tokens": 120,
        "output_tokens": 45,
        "total_tokens": 165,
        "latency_ms": 321,
    }


def test_collect_model_usage_returns_explicit_zero_record_when_metadata_absent() -> None:
    usage = collect_model_usage(
        messages=[],
        page_number=1,
        attempt_number=1,
        fallback_model_name="gpt-5.6-luna",
        latency_ms=0,
    )

    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.total_tokens == 0
    assert usage.provider_request_id is None
