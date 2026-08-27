"""Integration tests for PDFium validation and all-pages rendering."""

from __future__ import annotations

import io
import os
from pathlib import Path
from types import TracebackType

import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pypdfium2.raw as pdfium_raw  # type: ignore[import-untyped]
import pytest
from PIL import Image

from document_processing.pdf import (
    EncryptedPdfError,
    MalformedPdfError,
    PdfIntakeService,
    PdfPageLimitError,
    PdfRenderConfig,
    PdfRenderError,
    PdfStorageError,
    RenderedPage,
    RenderedPagePixelLimitError,
    ZeroPagePdfError,
)

from .helpers import PageSpec, make_pdf


def test_prepare_path_preserves_source_and_renders_ordered_rgb_pages(tmp_path: Path) -> None:
    source_path = tmp_path / "fixture.pdf"
    source = make_pdf(
        source_path,
        (
            PageSpec(width_points=72, height_points=36),
            PageSpec(width_points=36, height_points=72, rotation=90),
        ),
    )
    run_dir = tmp_path / "run"
    config = PdfRenderConfig(dpi=72)

    prepared = PdfIntakeService(config).prepare_path(source_path, run_dir)

    assert prepared.source_path.read_bytes() == source
    assert prepared.page_image_paths == (
        run_dir / "page_images/page-0001.png",
        run_dir / "page_images/page-0002.png",
    )
    assert prepared.manifest.page_count == 2
    assert prepared.manifest.configuration_fingerprint == config.fingerprint
    assert [page.page_number for page in prepared.manifest.pages] == [1, 2]
    assert [(page.width_px, page.height_px) for page in prepared.manifest.pages] == [
        (72, 36),
        (72, 36),
    ]
    for image_path in prepared.page_image_paths:
        with Image.open(image_path) as image:
            assert image.format == "PNG"
            assert image.mode == "RGB"


def test_render_honors_visible_crop_and_rotation(tmp_path: Path) -> None:
    source_path = tmp_path / "cropped.pdf"
    make_pdf(
        source_path,
        (
            PageSpec(
                width_points=144,
                height_points=72,
                rotation=90,
                crop_box=(36, 18, 108, 54),
            ),
        ),
    )

    prepared = PdfIntakeService(PdfRenderConfig(dpi=72)).prepare_path(source_path, tmp_path / "run")

    page = prepared.manifest.pages[0]
    assert (page.width_px, page.height_px) == (36, 72)


def test_worker_can_render_a_source_already_preserved_by_intake(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.pdf"
    source = make_pdf(fixture, (PageSpec(),))
    run_dir = tmp_path / "run"
    service = PdfIntakeService(PdfRenderConfig(dpi=72))
    stored = service.preserve_source(io.BytesIO(source), run_dir)

    prepared = service.prepare_path(stored.source_path, run_dir)

    assert prepared.document_id == stored.document_id
    assert prepared.page_image_paths == (run_dir / "page_images/page-0001.png",)


def test_renderer_atomically_replaces_repository_scaffold_image_directory(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "fixture.pdf"
    make_pdf(source_path, (PageSpec(),))
    run_dir = tmp_path / "run"
    (run_dir / "page_images").mkdir(parents=True)

    prepared = PdfIntakeService(PdfRenderConfig(dpi=72)).prepare_path(source_path, run_dir)

    assert prepared.page_image_paths[0].is_file()


def test_page_count_limit_fails_before_any_image_is_published(tmp_path: Path) -> None:
    source_path = tmp_path / "two-pages.pdf"
    make_pdf(source_path, (PageSpec(), PageSpec()))
    run_dir = tmp_path / "run"

    with pytest.raises(PdfPageLimitError):
        PdfIntakeService(PdfRenderConfig(max_pages=1)).prepare_path(source_path, run_dir)

    assert (run_dir / "source/original.pdf").is_file()
    assert not (run_dir / "page_images").exists()
    assert not (run_dir / "render_manifest.json").exists()


def test_pixel_limit_is_checked_before_bitmap_allocation(tmp_path: Path) -> None:
    source_path = tmp_path / "large-page.pdf"
    make_pdf(source_path, (PageSpec(width_points=72, height_points=72),))
    run_dir = tmp_path / "run"

    with pytest.raises(RenderedPagePixelLimitError):
        PdfIntakeService(PdfRenderConfig(dpi=72, max_page_pixels=5_000)).prepare_path(
            source_path, run_dir
        )

    assert not (run_dir / "page_images").exists()


def test_malformed_pdf_is_rejected_without_render_artifacts(tmp_path: Path) -> None:
    source_path = tmp_path / "malformed.pdf"
    source_path.write_bytes(b"%PDF-this-is-not-a-document")
    run_dir = tmp_path / "run"

    with pytest.raises(MalformedPdfError):
        PdfIntakeService().prepare_path(source_path, run_dir)

    assert (run_dir / "source/original.pdf").read_bytes() == source_path.read_bytes()
    assert not (run_dir / "page_images").exists()


def test_password_error_is_classified_without_leaking_provider_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "fixture.pdf"
    make_pdf(source_path, (PageSpec(),))

    def reject_password(_path: Path) -> None:
        raise pdfium.PdfiumError("sensitive password detail", pdfium_raw.FPDF_ERR_PASSWORD)

    monkeypatch.setattr(pdfium, "PdfDocument", reject_password)

    with pytest.raises(EncryptedPdfError, match="Password-protected") as caught:
        PdfIntakeService().prepare_path(source_path, tmp_path / "run")
    assert "sensitive" not in str(caught.value)


class EmptyDocument:
    def __enter__(self) -> EmptyDocument:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    def __len__(self) -> int:
        return 0


def test_zero_page_pdf_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_path = tmp_path / "fixture.pdf"
    make_pdf(source_path, (PageSpec(),))
    monkeypatch.setattr(pdfium, "PdfDocument", lambda _path: EmptyDocument())

    with pytest.raises(ZeroPagePdfError):
        PdfIntakeService().prepare_path(source_path, tmp_path / "run")


def test_page_images_publish_only_after_every_page_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "two-pages.pdf"
    make_pdf(source_path, (PageSpec(), PageSpec()))
    run_dir = tmp_path / "run"
    service = PdfIntakeService(PdfRenderConfig(dpi=72))
    render_page = service._render_page

    def fail_second_page(
        page: object,
        page_number: int,
        expected_size: tuple[int, int],
        output_dir: Path,
    ) -> RenderedPage:
        if page_number == 2:
            raise PdfRenderError("injected page failure")
        return render_page(page, page_number, expected_size, output_dir)

    monkeypatch.setattr(service, "_render_page", fail_second_page)

    with pytest.raises(PdfRenderError, match="injected"):
        service.prepare_path(source_path, run_dir)

    assert not (run_dir / "page_images").exists()
    assert not (run_dir / "render_manifest.json").exists()
    assert list((run_dir / ".staging").iterdir()) == []


def test_manifest_publication_failure_quarantines_unreferenced_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "fixture.pdf"
    source = make_pdf(source_path, (PageSpec(),))
    run_dir = tmp_path / "run"
    service = PdfIntakeService(PdfRenderConfig(dpi=72))
    stored = service.preserve_source(io.BytesIO(source), run_dir)
    real_link = os.link

    def fail_manifest_link(source: Path, target: Path, *, follow_symlinks: bool) -> None:
        if target.name == "render_manifest.json":
            raise OSError("injected manifest publication failure")
        real_link(source, target, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "link", fail_manifest_link)

    with pytest.raises(PdfStorageError):
        service.render_preserved(stored, run_dir)

    assert list((run_dir / "page_images").iterdir()) == []
    quarantined = list((run_dir / "quarantine").iterdir())
    assert len(quarantined) == 1
    assert (quarantined[0] / "page-0001.png").is_file()
    assert not (run_dir / "render_manifest.json").exists()
