"""Fail-closed verification for preserved and rendered PDF artifacts."""

from __future__ import annotations

import math
from pathlib import Path

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from .artifact_io import SOURCE_RELATIVE_PATH, hash_file
from .errors import RenderIntegrityError
from .models import PdfRenderConfig, PreparedPdf, RenderManifest, StoredPdf

MANIFEST_NAME = "render_manifest.json"
PAGE_IMAGES_DIRECTORY = "page_images"


def verify_prepared(
    run_dir: Path,
    config: PdfRenderConfig,
    expected_document_id: str | None = None,
) -> PreparedPdf:
    """Verify checksums, schemas, image decoding, and exact page correspondence."""

    run_dir = Path(run_dir)
    source = run_dir / SOURCE_RELATIVE_PATH
    manifest_path = run_dir / MANIFEST_NAME
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RenderIntegrityError("The render manifest is missing or unsafe.")
        manifest = RenderManifest.model_validate_json(manifest_path.read_bytes())
        if manifest.configuration_fingerprint != config.fingerprint:
            raise RenderIntegrityError("The render configuration fingerprint changed.")
        if expected_document_id and manifest.document_id != expected_document_id:
            raise RenderIntegrityError("The source document identity changed.")
        if source.parent.is_symlink() or source.is_symlink() or not source.is_file():
            raise RenderIntegrityError("The preserved source PDF is missing or unsafe.")
        _require_contained(source, run_dir)
        source_hash, source_size = hash_file(source, config.copy_chunk_bytes)
        if (source_hash, source_size) != (
            manifest.document_id,
            manifest.source_size_bytes,
        ):
            raise RenderIntegrityError("The preserved source PDF failed its checksum.")
        _verify_source_pages(source, manifest, config)
        _verify_images(run_dir, manifest, config)
        return prepared_result(
            StoredPdf(source_hash, source, source_size), manifest_path, manifest, run_dir
        )
    except RenderIntegrityError:
        raise
    except (OSError, ValidationError, ValueError, UnidentifiedImageError) as exc:
        raise RenderIntegrityError("Rendered PDF artifacts failed validation.") from exc


def prepared_result(
    stored: StoredPdf,
    manifest_path: Path,
    manifest: RenderManifest,
    run_dir: Path,
) -> PreparedPdf:
    """Construct ordered absolute artifact paths from a validated manifest."""

    return PreparedPdf(
        document_id=stored.document_id,
        source_path=stored.source_path,
        source_size_bytes=stored.source_size_bytes,
        manifest_path=manifest_path,
        manifest=manifest,
        page_image_paths=tuple(run_dir / page.image_path for page in manifest.pages),
    )


def _verify_images(
    run_dir: Path,
    manifest: RenderManifest,
    config: PdfRenderConfig,
) -> None:
    image_dir = run_dir / PAGE_IMAGES_DIRECTORY
    if image_dir.is_symlink() or not image_dir.is_dir():
        raise RenderIntegrityError("The rendered page image directory is missing or unsafe.")
    expected_names = {Path(page.image_path).name for page in manifest.pages}
    actual_names = {entry.name for entry in image_dir.iterdir()}
    if actual_names != expected_names:
        raise RenderIntegrityError("The rendered page image set does not match the manifest.")
    for page in manifest.pages:
        image_path = run_dir / page.image_path
        if image_path.is_symlink() or not image_path.is_file():
            raise RenderIntegrityError(f"Rendered page {page.page_number} is missing or unsafe.")
        digest, size = hash_file(image_path, config.copy_chunk_bytes)
        if (digest, size) != (page.image_sha256, page.image_size_bytes):
            raise RenderIntegrityError(f"Rendered page {page.page_number} failed its checksum.")
        if page.width_px * page.height_px > config.max_page_pixels:
            raise RenderIntegrityError(f"Rendered page {page.page_number} exceeds the pixel limit.")
        with Image.open(image_path) as image:
            if image.format != "PNG" or image.mode != "RGB":
                raise RenderIntegrityError(
                    f"Rendered page {page.page_number} has the wrong format."
                )
            if image.size != (page.width_px, page.height_px):
                raise RenderIntegrityError(f"Rendered page {page.page_number} changed dimensions.")
            image.verify()


def _verify_source_pages(
    source: Path,
    manifest: RenderManifest,
    config: PdfRenderConfig,
) -> None:
    try:
        document = pdfium.PdfDocument(source)
    except pdfium.PdfiumError as exc:
        raise RenderIntegrityError("The preserved source PDF is no longer readable.") from exc
    with document:
        if len(document) != manifest.page_count or len(document) > config.max_pages:
            raise RenderIntegrityError("The source page count does not match the render manifest.")
        for index, rendered in enumerate(manifest.pages):
            page = None
            try:
                page = document[index]
                width_points, height_points = page.get_size()
                if not all(
                    math.isfinite(value) and value > 0 for value in (width_points, height_points)
                ):
                    raise RenderIntegrityError(
                        f"Source page {rendered.page_number} has invalid dimensions."
                    )
                expected = (
                    math.ceil(width_points * config.scale),
                    math.ceil(height_points * config.scale),
                )
                if expected != (rendered.width_px, rendered.height_px):
                    raise RenderIntegrityError(
                        f"Source page {rendered.page_number} does not match "
                        "its rendered dimensions."
                    )
                if expected[0] * expected[1] > config.max_page_pixels:
                    raise RenderIntegrityError(
                        f"Source page {rendered.page_number} exceeds the pixel limit."
                    )
            except RenderIntegrityError:
                raise
            except (ValueError, pdfium.PdfiumError) as exc:
                raise RenderIntegrityError(
                    f"Source page {rendered.page_number} is no longer readable."
                ) from exc
            finally:
                if page is not None:
                    page.close()


def _require_contained(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise RenderIntegrityError("A PDF artifact path escapes its run directory.") from exc
