"""Failure classification used by the deterministic orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from httpx import NetworkError, TimeoutException
from langchain.agents.structured_output import StructuredOutputValidationError
from openai import APIConnectionError, APITimeoutError
from pydantic import ValidationError

from document_processing.processing.cancellation import ProcessingCancelled


class FailureCode(StrEnum):
    """Sanitized, durable processing failure categories."""

    CANCELLED = "cancelled"
    PDF_INTAKE_FAILED = "pdf_intake_failed"
    MODEL_TRANSIENT_EXHAUSTED = "model_transient_exhausted"
    MODEL_CONTRACT_INVALID = "model_contract_invalid"
    MODEL_PERMANENT = "model_permanent"
    STORAGE_FAILED = "storage_failed"
    INTEGRITY_FAILED = "integrity_failed"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True, slots=True)
class ProcessingFailure:
    """A provider-safe error suitable for status APIs and durable storage."""

    code: FailureCode
    message: str
    page_number: int | None = None
    retryable: bool = False
    exception_type: str | None = None


class PageContractError(ValueError):
    """A model response violated a deterministic page/state transition."""

    contract_error = True


def status_code_from_exception(error: BaseException) -> int | None:
    """Read the conventional HTTP status without depending on one SDK."""

    direct = getattr(error, "status_code", None)
    if isinstance(direct, int):
        return direct
    response: Any = getattr(error, "response", None)
    response_code = getattr(response, "status_code", None)
    return response_code if isinstance(response_code, int) else None


def is_transient_model_error(error: BaseException) -> bool:
    """Return true only for explicitly temporary transport/service failures."""

    if isinstance(error, ProcessingCancelled):
        return False
    if getattr(error, "retryable", None) is False:
        return False
    if isinstance(
        error,
        (
            TimeoutError,
            ConnectionError,
            APIConnectionError,
            APITimeoutError,
            NetworkError,
            TimeoutException,
        ),
    ):
        return True
    status_code = status_code_from_exception(error)
    return status_code in {408, 429} or (status_code is not None and 500 <= status_code <= 599)


def is_contract_error(error: BaseException) -> bool:
    """Recognize strict model/schema violations without importing the analyzer."""

    if isinstance(error, (ValidationError, StructuredOutputValidationError)):
        return True
    if getattr(error, "contract_error", False) is True:
        return True
    return type(error).__name__ in {
        "AnalyzerInputError",
        "AnalyzerResultError",
        "ModelCallLimitExceededError",
    }


def sanitized_failure(
    error: BaseException,
    *,
    page_number: int | None = None,
    attempts_exhausted: bool = False,
) -> ProcessingFailure:
    """Map an exception to a stable category without leaking provider details."""

    exception_type = type(error).__name__
    if isinstance(error, ProcessingCancelled):
        return ProcessingFailure(
            FailureCode.CANCELLED,
            "Processing was cancelled before the next page commit.",
            page_number,
            exception_type=exception_type,
        )
    if attempts_exhausted:
        return ProcessingFailure(
            FailureCode.MODEL_TRANSIENT_EXHAUSTED,
            "A temporary model failure persisted after all configured attempts.",
            page_number,
            retryable=True,
            exception_type=exception_type,
        )
    if is_contract_error(error):
        return ProcessingFailure(
            FailureCode.MODEL_CONTRACT_INVALID,
            "The page analysis did not satisfy the processing contract.",
            page_number,
            exception_type=exception_type,
        )
    return ProcessingFailure(
        FailureCode.MODEL_PERMANENT,
        "The model request failed with a non-retryable error.",
        page_number,
        exception_type=exception_type,
    )


def stage_failure(
    code: FailureCode,
    message: str,
    error: BaseException,
    *,
    page_number: int | None = None,
) -> ProcessingFailure:
    """Create a sanitized failure for a deterministic non-model stage."""

    return ProcessingFailure(
        code,
        message,
        page_number,
        exception_type=type(error).__name__,
    )


def commit_was_cancelled(error: BaseException) -> bool:
    """Recognize the processing or storage commit-boundary cancellation type."""

    return isinstance(error, ProcessingCancelled) or (
        type(error).__name__ == "CancelledBeforeCommit"
    )
