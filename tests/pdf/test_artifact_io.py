"""Tests for bounded, byte-exact source preservation."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

import pytest

from document_processing.pdf import (
    InvalidPdfSignatureError,
    PdfArtifactConflictError,
    PdfIntakeService,
    PdfRenderConfig,
    PdfStorageError,
    PdfUploadTooLargeError,
    UnsupportedPdfMediaTypeError,
)

from .helpers import PageSpec, make_pdf


def test_preserve_source_streams_bytes_and_sha256(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.pdf"
    source = make_pdf(fixture, (PageSpec(),))
    run_dir = tmp_path / "run"

    stored = PdfIntakeService().preserve_source(
        io.BytesIO(source), run_dir, media_type="application/pdf; charset=binary"
    )

    assert stored.source_path == run_dir / "source/original.pdf"
    assert stored.source_path.read_bytes() == source
    assert stored.source_size_bytes == len(source)
    assert stored.document_id == hashlib.sha256(source).hexdigest()
    assert list((run_dir / ".staging").iterdir()) == []


def test_preserve_source_rejects_wrong_media_type(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"

    with pytest.raises(UnsupportedPdfMediaTypeError):
        PdfIntakeService().preserve_source(
            io.BytesIO(b"%PDF-example"), run_dir, media_type="text/plain"
        )

    assert not (run_dir / "source/original.pdf").exists()


def test_preserve_source_rejects_invalid_signature(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"

    with pytest.raises(InvalidPdfSignatureError):
        PdfIntakeService().preserve_source(io.BytesIO(b"not a PDF"), run_dir)

    assert not (run_dir / "source/original.pdf").exists()


def test_preserve_source_enforces_streaming_limit(tmp_path: Path) -> None:
    config = PdfRenderConfig(max_upload_bytes=8, copy_chunk_bytes=4096)
    run_dir = tmp_path / "run"

    with pytest.raises(PdfUploadTooLargeError):
        PdfIntakeService(config).preserve_source(io.BytesIO(b"%PDF-1234"), run_dir)

    assert not (run_dir / "source/original.pdf").exists()


def test_preserve_source_never_overwrites_existing_source(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    service = PdfIntakeService()
    first = b"%PDF-first"
    service.preserve_source(io.BytesIO(first), run_dir)

    with pytest.raises(PdfArtifactConflictError):
        service.preserve_source(io.BytesIO(b"%PDF-second"), run_dir)

    assert (run_dir / "source/original.pdf").read_bytes() == first


def test_source_publication_race_cannot_overwrite_competing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    competing = b"competing-writer"

    def lose_race(_source: Path, target: Path, *, follow_symlinks: bool) -> None:
        assert follow_symlinks is False
        target.write_bytes(competing)
        raise FileExistsError(target)

    monkeypatch.setattr(os, "link", lose_race)

    with pytest.raises(PdfArtifactConflictError):
        PdfIntakeService().preserve_source(io.BytesIO(b"%PDF-original"), run_dir)

    assert (run_dir / "source/original.pdf").read_bytes() == competing


def test_preserve_source_rejects_symlinked_artifact_parent(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    outside = tmp_path / "outside"
    outside.mkdir()
    run_dir.mkdir()
    (run_dir / "source").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PdfStorageError, match="symbolic links"):
        PdfIntakeService().preserve_source(io.BytesIO(b"%PDF-source"), run_dir)

    assert not (outside / "original.pdf").exists()
