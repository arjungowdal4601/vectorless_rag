"""Public entry points for new and resumed document-processing runs."""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path

from document_processing.processing.cancellation import (
    CancellationToken,
    ProcessingCancelled,
)
from document_processing.processing.errors import (
    FailureCode,
    sanitized_failure,
    stage_failure,
)
from document_processing.processing.interfaces import (
    Analyzer,
    PdfIntake,
    ProcessingRepository,
)
from document_processing.processing.lifecycle import (
    CancellationScope,
    failed_result,
)
from document_processing.processing.models import ProcessingResult
from document_processing.processing.offload import BlockingCallTracker
from document_processing.processing.retry import (
    Clock,
    RandomSource,
    RetryPolicy,
    Sleeper,
)
from document_processing.processing.runner import ProcessingRunner
from document_processing.processing.source_checkpoint import (
    SourceCheckpointError,
    preserve_and_record_source,
)
from document_processing.processing.state import validate_prepared


class DocumentProcessor:
    """Create/resume runs and delegate their sequential page loop.

    Both public methods are asynchronous because page analysis is asynchronous.
    Synchronous PDF intake and repository calls run in tracked executor threads.
    """

    def __init__(
        self,
        *,
        analyzer: Analyzer,
        pdf_intake: PdfIntake,
        repository: ProcessingRepository,
        retry_policy: RetryPolicy | None = None,
        clock: Clock = time.monotonic,
        random_source: RandomSource = random.random,
        sleep: Sleeper = asyncio.sleep,
        offload_tracker: BlockingCallTracker | None = None,
    ) -> None:
        self._pdf_intake = pdf_intake
        self._repository = repository
        self._offloads = offload_tracker or BlockingCallTracker()
        self._runner = ProcessingRunner(
            analyzer=analyzer,
            repository=repository,
            retry_policy=retry_policy or RetryPolicy(),
            clock=clock,
            random_source=random_source,
            sleep=sleep,
            offloads=self._offloads,
        )

    async def drain_offloads(self) -> None:
        """Wait for every submitted intake or persistence call to leave its thread."""
        await self._offloads.drain()

    async def process_pdf(
        self,
        path: str | Path,
        cancellation: CancellationToken | None = None,
    ) -> ProcessingResult:
        """Create and fully process a durable run for one source PDF."""

        token = cancellation or CancellationToken()
        run = await self._offloads.run(self._repository.create_run)
        scope = CancellationScope(token, self._repository, run.run_id, self._offloads)
        document_id = run.document_id
        failure_code = FailureCode.STORAGE_FAILED
        failure_message = "The new run could not enter the running state."
        try:
            await scope.check()
            await self._offloads.run(
                self._repository.mark_run_running,
                run.run_id,
                resuming=False,
            )
            failure_code = FailureCode.PDF_INTAKE_FAILED
            failure_message = "The PDF source could not be preserved for processing."
            stored = await self._offloads.run(
                preserve_and_record_source,
                self._pdf_intake,
                self._repository,
                run,
                Path(path),
            )
            document_id = stored.document_id
            await scope.check()
            failure_code = FailureCode.PDF_INTAKE_FAILED
            failure_message = "The preserved PDF could not be rendered for processing."
            prepared = await self._offloads.run(
                self._pdf_intake.prepare_preserved_path,
                run.run_directory,
                expected_document_id=document_id,
            )
            validate_prepared(prepared, run.run_directory)
            failure_code = FailureCode.STORAGE_FAILED
            failure_message = "The prepared PDF state could not be committed durably."
            await self._offloads.run(
                self._repository.record_render_manifest,
                run.run_id,
                prepared,
            )
            checkpoint = await self._offloads.run(
                self._repository.initialize_run,
                run.run_id,
                page_count=prepared.manifest.page_count,
            )
        except ProcessingCancelled as error:
            return await failed_result(
                self._repository,
                run,
                document_id=document_id,
                completed=0,
                total=0,
                failure=sanitized_failure(error),
                offloads=self._offloads,
            )
        except Exception as error:
            if isinstance(error, SourceCheckpointError):
                document_id = error.document_id
                failure_code = FailureCode.STORAGE_FAILED
                failure_message = "The preserved PDF identity could not be committed durably."
            failure = stage_failure(
                failure_code,
                failure_message,
                error,
            )
            return await failed_result(
                self._repository,
                run,
                document_id=document_id,
                completed=0,
                total=0,
                failure=failure,
                offloads=self._offloads,
            )
        return await self._runner.run(run, prepared, checkpoint, scope)

    async def resume(
        self,
        run_id: str,
        cancellation: CancellationToken | None = None,
    ) -> ProcessingResult:
        """Audit a failed run and continue from its contiguous completed prefix."""

        token = cancellation or CancellationToken()
        return await self._resume_run(run_id, token, claim=True)

    async def _resume_run(
        self,
        run_id: str,
        token: CancellationToken,
        *,
        claim: bool,
    ) -> ProcessingResult:
        run = await self._offloads.run(self._repository.load_run, run_id)
        scope = CancellationScope(token, self._repository, run_id, self._offloads)
        failure_code = FailureCode.STORAGE_FAILED
        failure_message = "The run could not enter its resuming state."
        try:
            if claim:
                token.raise_if_cancelled()
                await self._offloads.run(
                    self._repository.mark_run_running,
                    run_id,
                    resuming=True,
                )
            else:
                await scope.check()
                if run.status != "resuming":
                    raise ValueError("service resume claim is no longer active")
            await scope.check()
            if run.document_id is None:
                raise ValueError("resumable run has no journaled source identity")
            failure_code = FailureCode.INTEGRITY_FAILED
            failure_message = "The run could not be resumed because its artifacts failed audit."
            if run.initialized or run.rendered:
                prepared = await self._offloads.run(
                    self._pdf_intake.verify_prepared,
                    run.run_directory,
                    expected_document_id=run.document_id,
                )
            else:
                prepared = await self._offloads.run(
                    self._pdf_intake.prepare_preserved_path,
                    run.run_directory,
                    expected_document_id=run.document_id,
                )
            await scope.check()
            validate_prepared(prepared, run.run_directory)
            if run.document_id is not None and prepared.document_id != run.document_id:
                raise ValueError("prepared document identity does not match the run")
            if run.initialized:
                checkpoint = await self._offloads.run(
                    self._repository.recover_run,
                    run_id,
                    prepared=prepared,
                )
            else:
                failure_code = FailureCode.STORAGE_FAILED
                failure_message = "The recovered render state could not be initialized durably."
                await self._offloads.run(
                    self._repository.record_render_manifest,
                    run_id,
                    prepared,
                )
                checkpoint = await self._offloads.run(
                    self._repository.initialize_run,
                    run_id,
                    page_count=prepared.manifest.page_count,
                )
            failure_code = FailureCode.STORAGE_FAILED
            failure_message = "The resumed run could not re-enter running state."
            await scope.check()
            await self._offloads.run(
                self._repository.mark_run_running,
                run_id,
                resuming=False,
            )
        except ProcessingCancelled as error:
            return await failed_result(
                self._repository,
                run,
                document_id=run.document_id,
                completed=0,
                total=0,
                failure=sanitized_failure(error),
                offloads=self._offloads,
            )
        except Exception as error:
            failure = stage_failure(
                failure_code,
                failure_message,
                error,
            )
            return await failed_result(
                self._repository,
                run,
                document_id=run.document_id,
                completed=0,
                total=0,
                failure=failure,
                offloads=self._offloads,
            )
        return await self._runner.run(run, prepared, checkpoint, scope)

    async def process_run(
        self,
        *,
        run_id: str,
        source_path: Path,
        cancellation: CancellationToken,
        resume: bool,
    ) -> ProcessingResult:
        """Process a service-registered run or resume its verified prefix."""

        if resume:
            return await self._resume_run(run_id, cancellation, claim=False)
        run = await self._offloads.run(self._repository.load_run, run_id)
        scope = CancellationScope(
            cancellation,
            self._repository,
            run_id,
            self._offloads,
        )
        failure_code = FailureCode.STORAGE_FAILED
        failure_message = "The registered run could not enter running state."
        try:
            await scope.check()
            if run.document_id is None:
                raise ValueError("registered run has no journaled source identity")
            if Path(source_path) != run.run_directory / "source/original.pdf":
                raise ValueError("registered source path is not canonical")
            await self._offloads.run(
                self._repository.mark_run_running,
                run_id,
                resuming=False,
            )
            failure_code = FailureCode.PDF_INTAKE_FAILED
            failure_message = "The registered PDF could not be rendered for processing."
            prepared = await self._offloads.run(
                self._pdf_intake.prepare_preserved_path,
                run.run_directory,
                expected_document_id=run.document_id,
            )
            await scope.check()
            validate_prepared(prepared, run.run_directory)
            if run.document_id is not None and prepared.document_id != run.document_id:
                raise ValueError("prepared document identity does not match the run")
            failure_code = FailureCode.STORAGE_FAILED
            failure_message = "The registered render state could not be committed durably."
            await self._offloads.run(
                self._repository.record_render_manifest,
                run_id,
                prepared,
            )
            checkpoint = await self._offloads.run(
                self._repository.initialize_run,
                run_id,
                page_count=prepared.manifest.page_count,
            )
        except ProcessingCancelled as error:
            return await failed_result(
                self._repository,
                run,
                document_id=run.document_id,
                completed=0,
                total=0,
                failure=sanitized_failure(error),
                offloads=self._offloads,
            )
        except Exception as error:
            failure = stage_failure(
                failure_code,
                failure_message,
                error,
            )
            return await failed_result(
                self._repository,
                run,
                document_id=run.document_id,
                completed=0,
                total=0,
                failure=failure,
                offloads=self._offloads,
            )
        return await self._runner.run(run, prepared, checkpoint, scope)
