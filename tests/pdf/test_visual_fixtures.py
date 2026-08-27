"""Visual PDF fixtures covering formula and table evidence."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from PIL import Image, ImageChops, ImageStat

from document_processing.pdf import PdfIntakeService, PdfRenderConfig

from .helpers import make_visual_content_pdf, visual_content_pages


def test_renderer_preserves_formula_and_table_visual_evidence_in_page_order(
    tmp_path: Path,
) -> None:
    """Compare rendered PNGs with the actual formula/table fixture canvases."""

    source_path = tmp_path / "formula-table.pdf"
    make_visual_content_pdf(source_path)

    prepared = PdfIntakeService(PdfRenderConfig(dpi=72)).prepare_path(source_path, tmp_path / "run")

    assert [page.page_number for page in prepared.manifest.pages] == [1, 2]
    assert prepared.page_image_paths == (
        tmp_path / "run/page_images/page-0001.png",
        tmp_path / "run/page_images/page-0002.png",
    )
    expected_pages = visual_content_pages()
    try:
        for expected, rendered_path in zip(expected_pages, prepared.page_image_paths, strict=True):
            with Image.open(rendered_path) as rendered:
                assert rendered.mode == "RGB"
                assert rendered.size == expected.size
                difference = ImageChops.difference(expected, rendered)
                assert max(ImageStat.Stat(difference).mean) < 1.0

        with Image.open(prepared.page_image_paths[0]) as formula:
            formula_marker = cast(tuple[int, int, int], formula.getpixel((20, 20)))
            assert formula_marker[0] > 150
            assert formula_marker[1] < 80
            assert _dark_pixel_count(formula.crop((55, 88, 305, 165))) > 400

        with Image.open(prepared.page_image_paths[1]) as table:
            table_marker = cast(tuple[int, int, int], table.getpixel((20, 20)))
            assert table_marker[1] > 100
            assert table_marker[0] < 80
            assert _dark_pixel_count(table.crop((20, 68, 340, 212))) > 3_000
    finally:
        for expected in expected_pages:
            expected.close()


def _dark_pixel_count(image: Image.Image) -> int:
    return sum(image.convert("L").histogram()[:120])
