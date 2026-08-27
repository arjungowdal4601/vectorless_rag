"""Small deterministic PDF fixtures created with the production renderer library."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True)
class PageSpec:
    width_points: float = 72
    height_points: float = 36
    rotation: int = 0
    crop_box: tuple[float, float, float, float] | None = None


def make_pdf(path: Path, pages: tuple[PageSpec, ...]) -> bytes:
    """Create a blank PDF with controlled page boxes and visible rotations."""

    document = pdfium.PdfDocument.new()
    try:
        for spec in pages:
            page = document.new_page(width=spec.width_points, height=spec.height_points)
            try:
                if spec.crop_box is not None:
                    page.set_cropbox(*spec.crop_box)
                if spec.rotation:
                    page.set_rotation(spec.rotation)
            finally:
                page.close()
        document.save(path)
    finally:
        document.close()
    return path.read_bytes()


def visual_content_pages() -> tuple[Image.Image, Image.Image]:
    """Build deterministic canvases with a formula and a real tabular layout."""

    return _formula_page(), _table_page()


def make_visual_content_pdf(path: Path) -> bytes:
    """Embed the visual fixture canvases as lossless PDF image objects."""

    pages = visual_content_pages()
    document = pdfium.PdfDocument.new()
    try:
        for canvas in pages:
            page = document.new_page(width=canvas.width, height=canvas.height)
            bitmap = pdfium.PdfBitmap.from_pil(canvas)
            try:
                page_image = pdfium.PdfImage.new(document)
                page_image.set_bitmap(bitmap)
                page_image.set_matrix(pdfium.PdfMatrix(canvas.width, 0, 0, canvas.height, 0, 0))
                page.insert_obj(page_image)
                page.gen_content()
            finally:
                bitmap.close()
                page.close()
        document.save(path)
    finally:
        document.close()
        for canvas in pages:
            canvas.close()
    return path.read_bytes()


def _formula_page() -> Image.Image:
    canvas = Image.new("RGB", (360, 240), "white")
    draw = ImageDraw.Draw(canvas)
    title_font = ImageFont.load_default(size=20)
    formula_font = ImageFont.load_default(size=24)
    note_font = ImageFont.load_default(size=13)

    draw.rectangle((12, 12, 92, 48), fill=(196, 38, 62))
    draw.text((108, 18), "FORMULA PAGE", fill=(20, 20, 25), font=title_font)
    draw.rounded_rectangle(
        (24, 72, 336, 190), radius=8, fill=(230, 240, 255), outline=(32, 74, 135), width=3
    )
    draw.text((64, 92), "E = m c^2", fill=(8, 22, 48), font=formula_font)
    draw.text((64, 132), "f(x) = (x^2 + 1) / (x - 1)", fill=(8, 22, 48), font=note_font)
    draw.line((64, 160, 294, 160), fill=(32, 74, 135), width=2)
    draw.text(
        (24, 211), "Visible equations are raster evidence.", fill=(45, 45, 52), font=note_font
    )
    return canvas


def _table_page() -> Image.Image:
    canvas = Image.new("RGB", (360, 240), "white")
    draw = ImageDraw.Draw(canvas)
    title_font = ImageFont.load_default(size=20)
    cell_font = ImageFont.load_default(size=13)

    draw.rectangle((12, 12, 92, 48), fill=(28, 132, 93))
    draw.text((108, 18), "TABLE PAGE", fill=(20, 20, 25), font=title_font)
    left, top, right, bottom = 24, 72, 336, 208
    draw.rectangle((left, top, right, 106), fill=(215, 239, 231))
    for x in (left, 104, 216, right):
        draw.line((x, top, x, bottom), fill=(18, 65, 54), width=3)
    for y in (top, 106, 140, 174, bottom):
        draw.line((left, y, right, y), fill=(18, 65, 54), width=3)
    rows = (
        ("Quarter", "Units", "Revenue"),
        ("Q1", "120", "$9,600"),
        ("Q2", "150", "$12,000"),
        ("Q3", "180", "$14,400"),
    )
    for row_index, row in enumerate(rows):
        baseline = 82 + (row_index * 34)
        for text, x in zip(row, (34, 122, 232), strict=True):
            draw.text((x, baseline), text, fill=(8, 38, 30), font=cell_font)
    return canvas
