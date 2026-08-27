"""Durable model-attempt allocation, retry, and cancellation behavior."""

from __future__ import annotations

from dataclasses import dataclass

from document_processing.contracts import EmptyShortTermMemory, ShortTermMemory
from document_processing.processing.cancellation import ProcessingCancelled
from document_processing.processing.errors import (
    FailureCode,
    ProcessingFailure,
    is_transient_model_error,
    sanitized_failure,
    stage_failure,
)
from document_processing.processing.interfaces import (
    AnalysisResult,
    Analyzer,
    PreparedPdf,
    ProcessingRepository,
)
from document_processing.processing.lifecycle import CancellationScope
from document_processing.processing.offload import BlockingCallTracker
from document_processing.processing.retry import (
    Clock,
    RandomSource,
    RetryPolicy,
    Sleeper,
    interruptible_async_backoff,
)
from document_processing.processing.state import memory_context


@dataclass(frozen=True, slots=True)
class SuccessfulAttempt:
    attempt_id: str
    result: AnalysisResult


class PageFailed(Exception):
    def __init__(self, failure: ProcessingFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


class PageAttemptRunner:
    """Issue one page request at a time within its durable retry budget."""

    def __init__(
        self,
        *,
        analyzer: Analyzer,
        repository: ProcessingRepository,
        retry_policy: RetryPolicy,
        clock: Clock,
        random_source: RandomSource,
        sleep: Sleeper,
        offloads: BlockingCallTracker,
    ) -> None:
        self._analyzer = analyzer
        self._repository = repository
        self._retry = retry_policy
        self._clock = clock
        self._random = random_source
        self._sleep = sleep
        self._offloads = offloads

    async def analyze(
        self,
        run_id: str,
        prepared: PreparedPdf,
        page_number: int,
        memory: EmptyShortTermMemory | ShortTermMemory,
        scope: CancellationScope,
        *,
        first_attempt_number: int,
    ) -> SuccessfulAttempt:
        if first_attempt_number > self._retry.max_attempts:
            raise PageFailed(
                ProcessingFailure(
                    FailureCode.MODEL_TRANSIENT_EXHAUSTED,
                    "The page exhausted its durable model-attempt budget.",
                    page_number,
                    retryable=True,
                )
            )
        for attempt_number in range(first_attempt_number, self._retry.max_attempts + 1):
            await scope.check()
            attempt = await self._offloads.run(
                self._repository.begin_page_attempt,
                run_id,
                page_number=page_number,
            )
            if attempt.attempt_number != attempt_number:
                failure = stage_failure(
                    FailureCode.INTEGRITY_FAILED,
                    "The durable model-attempt ordinal is contradictory.",
                    ValueError("repository returned an unexpected attempt ordinal"),
                    page_number=page_number,
                )
                await self.fail(run_id, attempt.attempt_id, failure)
                raise PageFailed(failure)
            try:
                result = await self._analyzer.analyze(
                    page_number=page_number,
                    page_image_path=prepared.page_image_paths[page_number - 1],
                    short_term_memory=memory_context(memory),
                    attempt_number=attempt_number,
                )
            except Exception as error:
                if await scope.is_cancelled():
                    await self.discard(run_id, attempt.attempt_id)
                    raise ProcessingCancelled("cancelled during model request") from error
                transient = is_transient_model_error(error)
                exhausted = transient and attempt_number == self._retry.max_attempts
                failure = sanitized_failure(
                    error,
                    page_number=page_number,
                    attempts_exhausted=exhausted,
                )
                will_retry = transient and not exhausted
                await self._offloads.run(
                    self._repository.fail_page_attempt,
                    run_id,
                    attempt_id=attempt.attempt_id,
                    failure=failure,
                    will_retry=will_retry,
                )
                if not will_retry:
                    raise PageFailed(failure) from error
                await interruptible_async_backoff(
                    self._retry.delay_after(attempt_number, self._random()),
                    cancelled=scope.is_cancelled,
                    clock=self._clock,
                    sleep=self._sleep,
                    poll_seconds=self._retry.cancellation_poll_seconds,
                )
                continue
            if await scope.is_cancelled():
                await self.discard(run_id, attempt.attempt_id)
                raise ProcessingCancelled("cancelled after model response")
            if (
                result.usage.page_number != page_number
                or result.usage.attempt_number != attempt_number
            ):
                failure = stage_failure(
                    FailureCode.INTEGRITY_FAILED,
                    "Analyzer usage metadata does not match the durable attempt.",
                    ValueError("analyzer usage ordinal mismatch"),
                    page_number=page_number,
                )
                await self.fail(run_id, attempt.attempt_id, failure)
                raise PageFailed(failure)
            return SuccessfulAttempt(attempt.attempt_id, result)
        raise AssertionError("retry loop exited without an outcome")

    async def discard(self, run_id: str, attempt_id: str | None) -> None:
        if attempt_id is not None:
            await self._offloads.run(
                self._repository.discard_page_attempt,
                run_id,
                attempt_id=attempt_id,
                reason="cancelled",
            )

    async def fail(
        self,
        run_id: str,
        attempt_id: str | None,
        failure: ProcessingFailure,
    ) -> None:
        if attempt_id is not None:
            await self._offloads.run(
                self._repository.fail_page_attempt,
                run_id,
                attempt_id=attempt_id,
                failure=failure,
                will_retry=False,
            )
