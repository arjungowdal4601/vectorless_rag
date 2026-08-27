"""Atomic upload preservation tests."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from document_processing.errors import InvalidPdfError, UploadTooLargeError
from document_processing.service.uploads import UploadPreserver


async def chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


def test_preserve_keeps_exact_bytes_and_hash(tmp_path: Path) -> None:
    async def scenario() -> None:
        payload = b"%PDF-1.7\nexact\x00bytes"
        result = await UploadPreserver(tmp_path, 1_000).preserve(
            "00000000-0000-4000-8000-000000000001", chunks(payload[:2], payload[2:])
        )

        assert result.path.read_bytes() == payload
        assert result.sha256 == hashlib.sha256(payload).hexdigest()
        assert result.size_bytes == len(payload)
        assert not (result.path.parents[1] / ".staging" / "source-upload.tmp").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [b"", b"not-a-pdf"])
def test_preserve_rejects_missing_pdf_signature(tmp_path: Path, payload: bytes) -> None:
    async def scenario() -> None:
        preserver = UploadPreserver(tmp_path, 1_000)
        with pytest.raises(InvalidPdfError):
            await preserver.preserve("00000000-0000-4000-8000-000000000002", chunks(payload))

    asyncio.run(scenario())


def test_preserve_enforces_limit_during_streaming(tmp_path: Path) -> None:
    async def scenario() -> None:
        with pytest.raises(UploadTooLargeError):
            await UploadPreserver(tmp_path, 6).preserve(
                "00000000-0000-4000-8000-000000000003", chunks(b"%PDF-", b"12")
            )

    asyncio.run(scenario())


def test_quarantine_moves_only_the_generated_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        run_id = "00000000-0000-4000-8000-000000000004"
        preserver = UploadPreserver(tmp_path, 1_000)
        result = await preserver.preserve(run_id, chunks(b"%PDF-fake"))
        destination = preserver.quarantine(run_id)

        assert not result.path.exists()
        assert (destination / "source" / "original.pdf").read_bytes() == b"%PDF-fake"

    asyncio.run(scenario())
