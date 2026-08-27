"""Bounded, atomic preservation of streamed PDF uploads."""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from document_processing.errors import (
    InvalidPdfError,
    ServiceNotReadyError,
    UploadTooLargeError,
)

PDF_SIGNATURE = b"%PDF-"


@dataclass(frozen=True, slots=True)
class PreservedUpload:
    path: Path
    sha256: str
    size_bytes: int


class UploadPreserver:
    """Write only service-generated paths and publish after a complete fsync."""

    def __init__(self, artifact_root: Path, max_upload_bytes: int) -> None:
        self._artifact_root = artifact_root
        self._max_upload_bytes = max_upload_bytes

    async def preserve(self, run_id: str, chunks: AsyncIterable[bytes]) -> PreservedUpload:
        runs_directory = self._artifact_root / "runs"
        runs_directory.mkdir(parents=True, exist_ok=True)
        if runs_directory.is_symlink() or not runs_directory.is_dir():
            raise ServiceNotReadyError("The run storage directory is unsafe.")
        run_directory = runs_directory / run_id
        source_directory = run_directory / "source"
        staging_directory = run_directory / ".staging"
        run_directory.mkdir(mode=0o700, exist_ok=False)
        for directory in (
            source_directory,
            run_directory / "page_images",
            run_directory / "page_artifacts",
            staging_directory,
            run_directory / "quarantine",
        ):
            directory.mkdir(mode=0o700)
        staging_path = staging_directory / "source-upload.tmp"
        source_path = source_directory / "original.pdf"
        digest = hashlib.sha256()
        prefix = bytearray()
        size = 0

        try:
            with staging_path.open("xb", buffering=0) as output:
                async for chunk in chunks:
                    self._validate_chunk(chunk)
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self._max_upload_bytes:
                        raise UploadTooLargeError(
                            f"The PDF exceeds the {self._max_upload_bytes}-byte upload limit."
                        )
                    if len(prefix) < len(PDF_SIGNATURE):
                        remaining = len(PDF_SIGNATURE) - len(prefix)
                        prefix.extend(chunk[:remaining])
                    digest.update(chunk)
                    output.write(chunk)
                self._validate_pdf_prefix(size, bytes(prefix))
                output.flush()
                os.fsync(output.fileno())
            os.replace(staging_path, source_path)
            self._fsync_directory(source_directory)
            self._fsync_directory(run_directory)
            self._fsync_directory(runs_directory)
        except BaseException:
            staging_path.unlink(missing_ok=True)
            raise

        return PreservedUpload(path=source_path, sha256=digest.hexdigest(), size_bytes=size)

    def quarantine(self, run_id: str) -> Path:
        """Move an unregistered invalid intake aside as a recoverable artifact."""

        run_directory = self._artifact_root / "runs" / run_id
        quarantine_root = self._artifact_root / "quarantine"
        quarantine_root.mkdir(parents=True, exist_ok=True)
        if quarantine_root.is_symlink() or not quarantine_root.is_dir():
            raise ServiceNotReadyError("The quarantine storage directory is unsafe.")
        destination = quarantine_root / f"invalid-intake-{run_id}"
        os.replace(run_directory, destination)
        self._fsync_directory(quarantine_root)
        self._fsync_directory(self._artifact_root / "runs")
        return destination

    @staticmethod
    def _validate_chunk(chunk: bytes) -> None:
        if not isinstance(chunk, bytes):
            raise TypeError("upload chunks must be bytes")

    @staticmethod
    def _validate_pdf_prefix(size: int, prefix: bytes) -> None:
        if size == 0:
            raise InvalidPdfError("The uploaded PDF is empty.")
        if prefix != PDF_SIGNATURE:
            raise InvalidPdfError("The uploaded file does not have a PDF signature.")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


async def file_chunks(path: Path, chunk_bytes: int = 1024 * 1024) -> AsyncIterator[bytes]:
    """Adapt a local file to the same bounded upload boundary as HTTP."""

    with path.open("rb") as source:
        while chunk := source.read(chunk_bytes):
            yield chunk
