"""Sequential page loop, retry handling, state commits, and final gating."""

from __future__ import annotations

from document_processing.processing.attempts import PageAttemptRunner, PageFailed
from document_processing.processing.cancellation import ProcessingCancelled
from document_processing.processing.completion import finalize_run
from document_processing.processing.errors import (
    FailureCode,
    PageContractError,
    commit_was_cancelled,
    sanitized_failure,
    stage_failure,
)
from document_processing.processing.interfaces import (
    Analyzer,
    PageCommit,
    PreparedPdf,
    ProcessingRepository,
    RecoveryCheckpoint,
    RunHandle,
)
from document_processing.processing.lifecycle import CancellationScope, failed_result
from document_processing.processing.models import ProcessingResult
from document_processing.processing.offload import BlockingCallTracker
from document_processing.processing.reconciliation import page_commit_is_durable
from document_processing.processing.retry import Clock, RandomSource, RetryPolicy, Sleeper
from document_processing.processing.state import materialize_page, validate_checkpoint


class ProcessingRunner:
    """Run only the verified sequential prefix-to-completion state machine."""

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
        self._repository = repository
        self._offloads = offloads
        self._attempts = PageAttemptRunner(
            analyzer=analyzer,
            repository=repository,
            retry_policy=retry_policy,
            clock=clock,
            random_source=random_source,
            sleep=sleep,
            offloads=offloads,
        )

    async def run(
        self,
        run: RunHandle,
        prepared: PreparedPdf,
        checkpoint: RecoveryCheckpoint,
        scope: CancellationScope,
    ) -> ProcessingResult:
        """Process every incomplete page and publish only an audited manifest."""

        total = prepared.manifest.page_count
        completed = checkpoint.completed_through
        try:
            validate_checkpoint(
                completed_through=checkpoint.completed_through,
                total_pages=total,
                short_term_memory=checkpoint.short_term_memory,
                document_structure=checkpoint.document_structure,
                next_attempt_number=checkpoint.next_attempt_number,
            )
        except Exception as error:
            failure = stage_failure(
                FailureCode.INTEGRITY_FAILED,
                "The recovered checkpoint is contradictory.",
                error,
            )
            return await failed_result(
                self._repository,
                run,
                document_id=prepared.document_id,
                completed=completed,
                total=total,
                failure=failure,
                offloads=self._offloads,
            )

        memory = checkpoint.short_term_memory
        structure = checkpoint.document_structure
        for page_number in range(checkpoint.next_page_number, total + 1):
            attempt_id: str | None = None
            commit_started = False
            try:
                await scope.check()
                first_attempt = (
                    checkpoint.next_attempt_number
                    if page_number == checkpoint.next_page_number
                    else 1
                )
                successful = await self._attempts.analyze(
                    run.run_id,
                    prepared,
                    page_number,
                    memory,
                    scope,
                    first_attempt_number=first_attempt,
                )
                attempt_id = successful.attempt_id
                await scope.check()
                page_entry = prepared.manifest.pages[page_number - 1]
                materialized = materialize_page(
                    successful.result.model_response,
                    page_number=page_number,
                    page_image_path=page_entry.image_path,
                    short_term_memory=memory,
                    document_structure=structure,
                )
                await scope.check()
                commit = PageCommit(
                    run_id=run.run_id,
                    attempt_id=attempt_id,
                    page_number=page_number,
                    page_artifact=materialized.page_artifact,
                    short_term_memory=materialized.short_term_memory,
                    document_structure=materialized.document_structure,
                    model_response=successful.result.model_response,
                    usage=successful.result.usage,
                )
                commit_started = True
                await self._offloads.run(
                    self._repository.commit_page,
                    commit,
                    cancel_check=scope.cancelled,
                )
            except PageFailed as error:
                return await failed_result(
                    self._repository,
                    run,
                    document_id=prepared.document_id,
                    completed=completed,
                    total=total,
                    failure=error.failure,
                    offloads=self._offloads,
                )
            except ProcessingCancelled as error:
                await self._attempts.discard(run.run_id, attempt_id)
                return await failed_result(
                    self._repository,
                    run,
                    document_id=prepared.document_id,
                    completed=completed,
                    total=total,
                    failure=sanitized_failure(error, page_number=page_number),
                    offloads=self._offloads,
                )
            except PageContractError as error:
                failure = sanitized_failure(error, page_number=page_number)
                await self._attempts.fail(run.run_id, attempt_id, failure)
                return await failed_result(
                    self._repository,
                    run,
                    document_id=prepared.document_id,
                    completed=completed,
                    total=total,
                    failure=failure,
                    offloads=self._offloads,
                )
            except Exception as error:
                if commit_was_cancelled(error):
                    failure = sanitized_failure(
                        ProcessingCancelled("cancelled at commit boundary"),
                        page_number=page_number,
                    )
                elif commit_started:
                    try:
                        committed = await page_commit_is_durable(
                            self._repository,
                            self._offloads,
                            prepared,
                            run_id=run.run_id,
                            page_number=page_number,
                            short_term_memory=materialized.short_term_memory,
                            document_structure=materialized.document_structure,
                        )
                    except Exception as recovery_error:
                        failure = stage_failure(
                            FailureCode.INTEGRITY_FAILED,
                            "The uncertain page commit could not be reconciled.",
                            recovery_error,
                            page_number=page_number,
                        )
                    else:
                        if committed:
                            memory = materialized.short_term_memory
                            structure = materialized.document_structure
                            completed = page_number
                            continue
                        failure = stage_failure(
                            FailureCode.STORAGE_FAILED,
                            "The page checkpoint could not be committed durably.",
                            error,
                            page_number=page_number,
                        )
                else:
                    failure = stage_failure(
                        FailureCode.STORAGE_FAILED,
                        "The page checkpoint could not be committed durably.",
                        error,
                        page_number=page_number,
                    )
                    await self._attempts.fail(run.run_id, attempt_id, failure)
                return await failed_result(
                    self._repository,
                    run,
                    document_id=prepared.document_id,
                    completed=completed,
                    total=total,
                    failure=failure,
                    offloads=self._offloads,
                )
            memory = materialized.short_term_memory
            structure = materialized.document_structure
            completed = page_number

        return await finalize_run(
            self._repository,
            run,
            prepared,
            completed,
            scope,
        )
