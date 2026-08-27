"""Durable, same-filesystem I/O primitives for PDF artifacts."""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO

from .errors import (
    InvalidPdfSignatureError,
    PdfArtifactConflictError,
    PdfStorageError,
    PdfUploadTooLargeError,
    UnsupportedPdfMediaTypeError,
)
from .models import PdfRenderConfig, StoredPdf

PDF_HEADER = b"%PDF-"
SOURCE_RELATIVE_PATH = Path("source/original.pdf")


def fsync_directory(path: Path) -> None:
    """Synchronize a directory entry after a rename or link operation."""

    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    """Return SHA-256 and byte size without loading an artifact into memory."""

    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def preserve_source(
    stream: BinaryIO,
    run_dir: Path,
    config: PdfRenderConfig,
    *,
    media_type: str | None = None,
) -> StoredPdf:
    """Copy, hash, and atomically preserve one source PDF byte-for-byte."""

    _validate_media_type(media_type)
    run_dir = Path(run_dir)
    source_dir = run_dir / SOURCE_RELATIVE_PATH.parent
    target = run_dir / SOURCE_RELATIVE_PATH
    staging_parent = run_dir / ".staging"
    if target.exists() or target.is_symlink():
        raise PdfArtifactConflictError("The immutable source PDF already exists.")
    if source_dir.is_symlink() or staging_parent.is_symlink():
        raise PdfStorageError("The PDF artifact directories must not be symbolic links.")

    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        source_dir.mkdir(parents=True, exist_ok=True)
        staging_parent.mkdir(parents=True, exist_ok=True)
        if source_dir.is_symlink() or staging_parent.is_symlink():
            raise PdfStorageError("The PDF artifact directories must not be symbolic links.")
        _require_contained(source_dir, run_dir)
        _require_contained(staging_parent, run_dir)
        fsync_directory(run_dir)
        stage_dir = staging_parent / f"source-{uuid.uuid4().hex}"
        stage_dir.mkdir()
        staged_source = stage_dir / "original.pdf"
        digest, size = _copy_source(stream, staged_source, config)
        with staged_source.open("rb") as staged_input:
            if staged_input.read(len(PDF_HEADER)) != PDF_HEADER:
                raise InvalidPdfSignatureError("The upload does not have a PDF header.")
        try:
            os.link(staged_source, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise PdfArtifactConflictError("The immutable source PDF already exists.") from exc
        fsync_directory(source_dir)
        return StoredPdf(digest, target, size)
    except (
        InvalidPdfSignatureError,
        PdfArtifactConflictError,
        PdfUploadTooLargeError,
        UnsupportedPdfMediaTypeError,
    ):
        raise
    except OSError as exc:
        raise PdfStorageError("The source PDF could not be stored.") from exc
    finally:
        if "stage_dir" in locals():
            shutil.rmtree(stage_dir, ignore_errors=True)


def _copy_source(
    stream: BinaryIO,
    destination: Path,
    config: PdfRenderConfig,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with destination.open("xb") as output:
        while True:
            chunk = stream.read(config.copy_chunk_bytes)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise TypeError("PDF input streams must return bytes.")
            size += len(chunk)
            if size > config.max_upload_bytes:
                raise PdfUploadTooLargeError(
                    f"The PDF exceeds the {config.max_upload_bytes}-byte upload limit."
                )
            digest.update(chunk)
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    return digest.hexdigest(), size


def _validate_media_type(media_type: str | None) -> None:
    if media_type is None:
        return
    normalized = media_type.partition(";")[0].strip().lower()
    if normalized != "application/pdf":
        raise UnsupportedPdfMediaTypeError("Only application/pdf uploads are accepted.")


def _require_contained(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise PdfStorageError("A PDF artifact path escapes its run directory.") from exc
