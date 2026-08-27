"""Focused tests for transient classification and cooperative waits."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import httpx
import pytest
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.structured_output import StructuredOutputValidationError
from langchain_core.messages import AIMessage
from openai import APIConnectionError, APITimeoutError

from document_processing.processing.cancellation import (
    CancellationToken,
    ProcessingCancelled,
)
from document_processing.processing.errors import is_contract_error, is_transient_model_error
from document_processing.processing.retry import RetryPolicy, interruptible_backoff
from tests.processing.fakes import HttpFailure


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


def test_retry_delay_is_bounded_exponential_with_symmetric_jitter() -> None:
    policy = RetryPolicy(
        base_delay_seconds=2,
        max_delay_seconds=5,
        jitter_ratio=0.25,
    )

    assert policy.delay_after(1, 0) == 1.5
    assert policy.delay_after(1, 0.5) == 2
    assert policy.delay_after(2, 1) == 5
    assert policy.delay_after(4, 0.5) == 5


def test_retry_policy_cannot_exceed_three_total_attempts() -> None:
    with pytest.raises(ValueError, match="cannot exceed three"):
        RetryPolicy(max_attempts=4)


@pytest.mark.parametrize("status", [408, 429, 500, 503, 599])
def test_transient_http_statuses_are_retryable(status: int) -> None:
    assert is_transient_model_error(HttpFailure(status))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_permanent_http_statuses_are_not_retryable(status: int) -> None:
    assert not is_transient_model_error(HttpFailure(status))


def test_openai_and_httpx_transport_failures_are_retryable() -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    connection_error = APIConnectionError.__new__(APIConnectionError)
    timeout_error = APITimeoutError.__new__(APITimeoutError)
    Exception.__init__(connection_error, "connection failed")
    Exception.__init__(timeout_error, "timed out")
    errors = (
        connection_error,
        timeout_error,
        httpx.ReadTimeout("timed out", request=request),
        httpx.NetworkError("network failed", request=request),
    )

    assert all(is_transient_model_error(error) for error in errors)


def test_explicit_non_retryable_marker_wins_over_status() -> None:
    error = HttpFailure(503)
    error.retryable = False  # type: ignore[attr-defined]

    assert not is_transient_model_error(error)


def test_retryable_marker_alone_does_not_broaden_transient_allowlist() -> None:
    error = RuntimeError("permanent")
    error.retryable = True  # type: ignore[attr-defined]

    assert not is_transient_model_error(error)


def test_provider_structured_output_validation_is_a_contract_error() -> None:
    error = StructuredOutputValidationError(
        "ModelResponse",
        ValueError("invalid provider payload"),
        AIMessage(content=""),
    )

    assert is_contract_error(error)
    assert is_contract_error(ModelCallLimitExceededError(0, 1, None, 1))


def test_cancellation_token_is_idempotent_and_interrupts_wait() -> None:
    token = CancellationToken()
    token.cancel("first")
    token.cancel("second")
    assert token.reason == "first"
    assert run(token.wait_cancelled(0.01))
    with pytest.raises(ProcessingCancelled):
        token.raise_if_cancelled()


def test_interruptible_backoff_checks_during_sleep() -> None:
    token = CancellationToken()
    now = 0.0

    def clock() -> float:
        return now

    async def sleep(delay: float) -> None:
        nonlocal now
        now += delay
        token.cancel()

    with pytest.raises(ProcessingCancelled):
        run(
            interruptible_backoff(
                2,
                cancelled=lambda: token.is_cancelled,
                clock=clock,
                sleep=sleep,
                poll_seconds=0.1,
            )
        )

    assert now == 0.1
