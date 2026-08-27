"""ASGI ingress guard that bounds multipart spooling before form parsing."""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from document_processing.errors import UploadTooLargeError

MULTIPART_OVERHEAD_BYTES = 64 * 1024


class UploadBodyLimitMiddleware:
    """Cap the entire upload request slightly above the configured file limit."""

    def __init__(self, app: ASGIApp, *, max_file_bytes: int) -> None:
        self._app = app
        self._max_body_bytes = max_file_bytes + MULTIPART_OVERHEAD_BYTES

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/v1/runs":
            await self._app(scope, receive, send)
            return
        declared = _content_length(scope)
        if declared is not None and declared > self._max_body_bytes:
            await _too_large_response(scope, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_body_bytes:
                    raise UploadTooLargeError(
                        "The multipart upload exceeds the configured request limit."
                    )
            return message

        await self._app(scope, limited_receive, send)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name.lower() != b"content-length":
            continue
        try:
            parsed = int(value.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return None
        return parsed if parsed >= 0 else None
    return None


async def _too_large_response(scope: Scope, send: Send) -> None:
    path = str(scope.get("path", "/v1/runs"))
    payload: dict[str, Any] = {
        "type": "urn:document-processing:error:upload_too_large",
        "title": "Upload too large",
        "status": 413,
        "detail": "The multipart upload exceeds the configured request limit.",
        "instance": path,
        "code": "upload_too_large",
    }
    response = JSONResponse(
        payload,
        status_code=413,
        media_type="application/problem+json",
    )
    await response(scope, _empty_receive, send)


async def _empty_receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}
