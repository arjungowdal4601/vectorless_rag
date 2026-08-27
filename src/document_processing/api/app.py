"""Application factory and versioned HTTP routes."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from document_processing.api.body_limit import UploadBodyLimitMiddleware
from document_processing.api.contracts import (
    LocalRunServiceProtocol,
    ServiceError,
)
from document_processing.api.problems import problem_response
from document_processing.errors import DocumentProcessingError, UploadTooLargeError

PDF_MEDIA_TYPES = frozenset({"application/pdf", "application/x-pdf"})
RUN_STATUSES = frozenset({"not_started", "running", "resuming", "completed", "failed"})
UPLOAD_CHUNK_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _object_payload(value: Any) -> dict[str, Any]:
    encoded = jsonable_encoder(value)
    if not isinstance(encoded, dict):
        raise ServiceError(
            "internal_error",
            "The run service returned an invalid response.",
        )
    return encoded


def _json(value: Any, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=jsonable_encoder(value))


def _run_payload(value: Any) -> dict[str, Any]:
    payload = _object_payload(value)
    if payload.get("status") not in RUN_STATUSES:
        raise ServiceError(
            "internal_error",
            "The run service returned an invalid run status.",
        )
    run_id = payload.get("run_id")
    document_id = payload.get("document_id")
    try:
        parsed_run_id = UUID(run_id) if isinstance(run_id, str) else None
    except ValueError:
        parsed_run_id = None
    if parsed_run_id is None or str(parsed_run_id) != run_id:
        raise ServiceError("internal_error", "The run service returned an invalid run ID.")
    if not isinstance(document_id, str) or _SHA256.fullmatch(document_id) is None:
        raise ServiceError(
            "internal_error",
            "The run service returned an invalid document ID.",
        )
    return payload


async def _upload_chunks(upload: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
        yield chunk


def _http_problem_code(status: int) -> str:
    if status == 404:
        return "not_found"
    if status == 405:
        return "method_not_allowed"
    return "invalid_request" if status < 500 else "internal_error"


def create_app(
    service: LocalRunServiceProtocol,
    *,
    max_upload_bytes: int | None = None,
) -> FastAPI:
    """Build the API around an already-configured local run service."""

    app = FastAPI(
        title="Document Processing API",
        version="1.0.0",
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.run_service = service
    settings = getattr(service, "settings", None)
    discovered_limit = getattr(settings, "max_upload_bytes", None)
    configured_limit = (
        max_upload_bytes
        if max_upload_bytes is not None
        else discovered_limit
        if isinstance(discovered_limit, int)
        else 100 * 1024 * 1024
    )
    app.add_middleware(
        UploadBodyLimitMiddleware,
        max_file_bytes=configured_limit,
    )
    discovered_page_default = getattr(settings, "page_list_default_limit", None)
    discovered_page_max = getattr(settings, "page_list_max_limit", None)
    page_default = discovered_page_default if isinstance(discovered_page_default, int) else 100
    page_max = discovered_page_max if isinstance(discovered_page_max, int) else 500
    if page_default < 1 or page_max < page_default:
        raise ValueError("page pagination settings are inconsistent")

    @app.exception_handler(DocumentProcessingError)
    async def processing_error_handler(
        request: Request,
        exc: DocumentProcessingError,
    ) -> JSONResponse:
        headers = {"Retry-After": "1"} if exc.code == "queue_full" else None
        return problem_response(
            request,
            exc.code,
            exc.message,
            status=exc.status_code,
            run_id=exc.run_id,
            headers=headers,
        )

    @app.exception_handler(ServiceError)
    async def service_error_handler(
        request: Request,
        exc: ServiceError,
    ) -> JSONResponse:
        headers = {"Retry-After": "1"} if exc.code == "queue_full" else None
        return problem_response(
            request,
            exc.code,
            exc.detail,
            run_id=exc.run_id,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return problem_response(
            request,
            "validation_error",
            "The request parameters do not satisfy the API contract.",
            extensions={"errors": exc.errors()},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        return problem_response(
            request,
            _http_problem_code(exc.status_code),
            str(exc.detail),
            status=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(
        request: Request,
        _exc: Exception,
    ) -> JSONResponse:
        return problem_response(
            request,
            "internal_error",
            "An unexpected error occurred while handling the request.",
        )

    @app.post("/v1/runs", status_code=202)
    async def submit_run(request: Request) -> JSONResponse:
        request_media_type = (
            request.headers.get("content-type", "").partition(";")[0].strip().lower()
        )
        if request_media_type != "multipart/form-data":
            raise ServiceError(
                "unsupported_media_type",
                "Use multipart/form-data with exactly one PDF file field.",
            )

        try:
            form = await request.form()
        except UploadTooLargeError:
            raise
        except Exception as exc:
            raise ServiceError(
                "invalid_request",
                "The multipart request could not be parsed.",
            ) from exc
        parts = form.multi_items()
        if len(parts) != 1 or parts[0][0] != "file" or not isinstance(parts[0][1], UploadFile):
            await form.close()
            raise ServiceError(
                "invalid_request",
                "Provide exactly one multipart file field named 'file'.",
            )

        upload = parts[0][1]
        file_media_type = (upload.content_type or "").partition(";")[0].strip().lower()
        if file_media_type not in PDF_MEDIA_TYPES:
            await upload.close()
            raise ServiceError(
                "unsupported_media_type",
                "The file field must declare content type application/pdf.",
            )
        if not upload.filename:
            await upload.close()
            raise ServiceError("invalid_request", "The PDF must have a filename.")

        try:
            run = await service.submit_pdf(
                filename=upload.filename,
                content_type=file_media_type,
                chunks=_upload_chunks(upload),
            )
        finally:
            await upload.close()

        payload = _run_payload(run)
        run_id = payload.get("run_id")
        document_id = payload.get("document_id")
        status = payload.get("status")
        if not all(isinstance(value, str) for value in (run_id, document_id, status)):
            raise ServiceError(
                "internal_error",
                "The run service returned incomplete acceptance data.",
            )
        return _json(
            {
                "run_id": run_id,
                "document_id": document_id,
                "status": status,
                "status_url": f"/v1/runs/{run_id}",
                "pages_url": f"/v1/runs/{run_id}/pages",
                "manifest_url": f"/v1/runs/{run_id}/manifest",
            },
            status_code=202,
        )

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str) -> JSONResponse:
        return _json(_run_payload(await service.get_run(run_id)))

    @app.get("/v1/runs/{run_id}/pages")
    async def list_pages(
        run_id: str,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: int = Query(default=page_default, ge=1, le=page_max),
    ) -> JSONResponse:
        result = await service.list_pages(run_id, offset=offset, limit=limit)
        return _json(result)

    @app.post("/v1/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(run_id: str) -> JSONResponse:
        result = _run_payload(await service.cancel_run(run_id))
        return _json(result, status_code=202)

    @app.post("/v1/runs/{run_id}/resume", status_code=202)
    async def resume_run(run_id: str) -> JSONResponse:
        result = _run_payload(await service.resume_run(run_id))
        return _json(result, status_code=202)

    @app.get("/v1/runs/{run_id}/manifest")
    async def get_manifest(run_id: str) -> JSONResponse:
        run = _run_payload(await service.get_run(run_id))
        if run.get("status") != "completed":
            raise ServiceError(
                "result_not_ready",
                "The manifest is available only after processing completes.",
                run_id=run_id,
            )
        return _json(await service.get_manifest(run_id))

    @app.get("/healthz")
    async def health() -> JSONResponse:
        return _json({"status": "ok"})

    @app.get("/readyz")
    async def readiness(request: Request) -> JSONResponse:
        result = await service.readiness()
        payload = _object_payload(result)
        ready = payload.get("ready")
        if not isinstance(ready, bool):
            raise ServiceError(
                "internal_error",
                "The run service returned invalid readiness data.",
            )
        if not ready:
            reason = payload.get("reason")
            detail = "The local processing service is not ready."
            if isinstance(reason, str) and reason:
                detail = f"The local processing service is not ready ({reason})."
            code = (
                "storage_unavailable" if reason == "storage_unavailable" else "worker_unavailable"
            )
            return problem_response(request, code, detail)
        return _json(payload)

    return app
