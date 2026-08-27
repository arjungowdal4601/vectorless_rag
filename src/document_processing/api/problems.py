"""RFC 9457-style problem details used by every API error response."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

ERROR_STATUS: dict[str, int] = {
    "invalid_request": 400,
    "validation_error": 422,
    "unsupported_media_type": 415,
    "upload_too_large": 413,
    "invalid_pdf": 422,
    "encrypted_pdf": 422,
    "page_limit_exceeded": 422,
    "render_size_limit_exceeded": 422,
    "run_not_found": 404,
    "result_not_ready": 409,
    "invalid_run_state": 409,
    "run_not_resumable": 409,
    "queue_full": 429,
    "storage_unavailable": 503,
    "worker_unavailable": 503,
    "not_found": 404,
    "method_not_allowed": 405,
    "internal_error": 500,
}

ERROR_TITLE: dict[str, str] = {
    "invalid_request": "Invalid request",
    "validation_error": "Request validation failed",
    "unsupported_media_type": "Unsupported media type",
    "upload_too_large": "Upload too large",
    "invalid_pdf": "Invalid PDF",
    "encrypted_pdf": "Encrypted PDF",
    "page_limit_exceeded": "PDF page limit exceeded",
    "render_size_limit_exceeded": "Rendered page size limit exceeded",
    "run_not_found": "Run not found",
    "result_not_ready": "Result not ready",
    "invalid_run_state": "Invalid run state",
    "run_not_resumable": "Run cannot be resumed",
    "queue_full": "Processing queue is full",
    "storage_unavailable": "Storage unavailable",
    "worker_unavailable": "Worker unavailable",
    "not_found": "Not found",
    "method_not_allowed": "Method not allowed",
    "internal_error": "Internal server error",
}


def problem_response(
    request: Request,
    code: str,
    detail: str,
    *,
    status: int | None = None,
    run_id: str | None = None,
    headers: Mapping[str, str] | None = None,
    extensions: Mapping[str, Any] | None = None,
) -> JSONResponse:
    """Create the API's canonical problem response."""

    resolved_status = status or ERROR_STATUS.get(code, 500)
    body: dict[str, Any] = {
        "type": f"urn:document-processing:error:{code}",
        "title": ERROR_TITLE.get(code, "Request failed"),
        "status": resolved_status,
        "detail": detail,
        "instance": request.url.path,
        "code": code,
    }
    if run_id is not None:
        body["run_id"] = run_id
    if extensions:
        body.update(extensions)
    return JSONResponse(
        status_code=resolved_status,
        content=jsonable_encoder(body),
        media_type="application/problem+json",
        headers=dict(headers or {}),
    )
