"""PDFium-backed source intake, page rendering, and integrity verification."""

from __future__ import annotations

import json
import math
import os
import shutil
import uuid
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO

import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pypdfium2.raw as pdfium_raw  # type: ignore[import-untyped]

from .artifact_io import (
    PDF_HEADER,
    SOURCE_RELATIVE_PATH,
    fsync_directory,
    hash_file,
    preserve_source,
)
from .errors import (
    EncryptedPdfError,
    InvalidPdfSignatureError,
    MalformedPdfError,
    PdfArtifactConflictError,
    PdfPageLimitError,
    PdfProcessingError,
    PdfRenderError,
    PdfStorageError,
    PdfUploadTooLargeError,
    RenderedPagePixelLimitError,
    RenderIntegrityError,
    ZeroPagePdfError,
)
from .models import PdfRenderConfig, PreparedPdf, RenderedPage, RenderManifest, StoredPdf
from .recovery import quarantine_unreferenced_render
from .verifier import prepared_result, verify_prepared

MANIFEST_NAME = "render_manifest.json"
PAGE_IMAGES_DIRECTORY = "page_images"


class PdfIntakeService:
    """Preserve one PDF, render every page, and expose ordered artifacts."""

    def __init__(self, config: PdfRenderConfig | None = None) -> None:
        self.config = config or PdfRenderConfig()

    def prepare(
        self,
        stream: BinaryIO,
        run_dir: Path,
        *,
        media_type: str | None = None,
    ) -> PreparedPdf:
        """Preserve a stream and render it completely before returning."""

        stored = self.preserve_source(stream, run_dir, media_type=media_type)
        return self.render_preserved(stored, run_dir)

    def prepare_path(self, path: Path, run_dir: Path) -> PreparedPdf:
        """Path-friendly adapter used by the reusable processing library."""

        stored = self.preserve_path(path, run_dir)
        return self.render_preserved(stored, Path(run_dir))

    def preserve_path(self, path: Path, run_dir: Path) -> StoredPdf:
        """Path-friendly source preservation without rendering."""

        path = Path(path)
        run_dir = Path(run_dir)
        try:
            if path.stat().st_size > self.config.max_upload_bytes:
                raise PdfUploadTooLargeError(
                    f"The PDF exceeds the {self.config.max_upload_bytes}-byte upload limit."
                )
            if path.resolve() == (run_dir / SOURCE_RELATIVE_PATH).resolve():
                digest, size = hash_file(path, self.config.copy_chunk_bytes)
                return StoredPdf(digest, path, size)
            with path.open("rb") as stream:
                return self.preserve_source(stream, run_dir)
        except PdfProcessingError:
            raise
        except OSError as exc:
            raise PdfStorageError("The source PDF could not be read.") from exc

    def prepare_preserved_path(
        self,
        run_dir: Path,
        expected_document_id: str | None = None,
    ) -> PreparedPdf:
        """Render a source already committed by durable HTTP intake."""

        run_dir = Path(run_dir)
        source_path = run_dir / SOURCE_RELATIVE_PATH
        try:
            digest, size = hash_file(source_path, self.config.copy_chunk_bytes)
        except OSError as exc:
            raise PdfStorageError("The preserved source PDF could not be read.") from exc
        if expected_document_id is not None and digest != expected_document_id:
            raise RenderIntegrityError("The source document identity changed.")
        return self.render_preserved(StoredPdf(digest, source_path, size), run_dir)

    def preserve_source(
        self,
        stream: BinaryIO,
        run_dir: Path,
        *,
        media_type: str | None = None,
    ) -> StoredPdf:
        """Expose the source commit separately for queued HTTP intake."""

        return preserve_source(stream, Path(run_dir), self.config, media_type=media_type)

    def render_preserved(self, stored: StoredPdf, run_dir: Path) -> PreparedPdf:
        """Validate a preserved source and atomically publish all page images."""

        run_dir = Path(run_dir)
        self._verify_stored_source(stored, run_dir)
        image_target = run_dir / PAGE_IMAGES_DIRECTORY
        manifest_target = run_dir / MANIFEST_NAME
        image_target_present = image_target.exists() or image_target.is_symlink()
        image_target_conflicts = image_target_present and (
            image_target.is_symlink() or not image_target.is_dir() or any(image_target.iterdir())
        )
        manifest_target_present = manifest_target.exists() or manifest_target.is_symlink()
        if image_target_conflicts or manifest_target_present:
            raise PdfArtifactConflictError("Rendered PDF artifacts already exist.")

        staging_parent = run_dir / ".staging"
        if staging_parent.is_symlink():
            raise PdfStorageError("The PDF staging directory must not be a symbolic link.")
        stage_dir = staging_parent / f"render-{uuid.uuid4().hex}"
        stage_images = stage_dir / PAGE_IMAGES_DIRECTORY
        try:
            staging_parent.mkdir(parents=True, exist_ok=True)
            stage_images.mkdir(parents=True)
            pages = self._render_all(stored.source_path, stage_images)
            manifest = self._build_manifest(stored, pages)
            staged_manifest = stage_dir / MANIFEST_NAME
            self._write_manifest(staged_manifest, manifest)
            fsync_directory(stage_images)
            fsync_directory(stage_dir)
            os.replace(stage_images, image_target)
            fsync_directory(run_dir)
            try:
                os.link(staged_manifest, manifest_target, follow_symlinks=False)
            except FileExistsError as exc:
                raise PdfArtifactConflictError("Rendered PDF artifacts already exist.") from exc
            fsync_directory(run_dir)
        except PdfProcessingError:
            raise
        except OSError as exc:
            if not (manifest_target.exists() or manifest_target.is_symlink()):
                with suppress(PdfProcessingError):
                    self.quarantine_unreferenced_render(run_dir)
            raise PdfStorageError("Rendered PDF artifacts could not be stored.") from exc
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

        return prepared_result(stored, manifest_target, manifest, run_dir)

    def quarantine_unreferenced_render(self, run_dir: Path) -> Path | None:
        """Quarantine images only after the journal proves no manifest references them."""

        return quarantine_unreferenced_render(Path(run_dir))

    def verify_prepared(
        self,
        run_dir: Path,
        expected_document_id: str | None = None,
    ) -> PreparedPdf:
        """Fail closed unless the source, manifest, and exact page set agree."""

        return verify_prepared(Path(run_dir), self.config, expected_document_id)

    def _verify_stored_source(self, stored: StoredPdf, run_dir: Path) -> None:
        expected_path = run_dir / SOURCE_RELATIVE_PATH
        try:
            if stored.source_path.resolve() != expected_path.resolve():
                raise RenderIntegrityError("The preserved source path is not canonical.")
            if (
                stored.source_path.parent.is_symlink()
                or stored.source_path.is_symlink()
                or not stored.source_path.is_file()
            ):
                raise RenderIntegrityError("The preserved source PDF is missing or unsafe.")
            digest, size = hash_file(stored.source_path, self.config.copy_chunk_bytes)
            if (digest, size) != (stored.document_id, stored.source_size_bytes):
                raise RenderIntegrityError("The preserved source PDF failed its checksum.")
            if size > self.config.max_upload_bytes:
                raise PdfUploadTooLargeError(
                    f"The PDF exceeds the {self.config.max_upload_bytes}-byte upload limit."
                )
            with stored.source_path.open("rb") as stream:
                if stream.read(len(PDF_HEADER)) != PDF_HEADER:
                    raise InvalidPdfSignatureError("The upload does not have a PDF header.")
        except PdfProcessingError:
            raise
        except OSError as exc:
            raise PdfStorageError("The preserved source PDF could not be read.") from exc

    def _render_all(self, source: Path, output_dir: Path) -> tuple[RenderedPage, ...]:
        try:
            document = pdfium.PdfDocument(source)
        except pdfium.PdfiumError as exc:
            if exc.err_code == pdfium_raw.FPDF_ERR_PASSWORD:
                raise EncryptedPdfError("Password-protected PDFs are not supported.") from exc
            raise MalformedPdfError("The source is not a readable PDF document.") from exc

        with document:
            page_count = len(document)
            if page_count == 0:
                raise ZeroPagePdfError("PDFs must contain at least one page.")
            if page_count > self.config.max_pages:
                raise PdfPageLimitError(f"The PDF exceeds the {self.config.max_pages}-page limit.")
            rendered = []
            for index in range(page_count):
                page_number = index + 1
                page = None
                try:
                    page = document[index]
                    expected_size = self._expected_pixel_size(page, page_number)
                    rendered.append(self._render_page(page, page_number, expected_size, output_dir))
                except PdfProcessingError:
                    raise
                except (ValueError, pdfium.PdfiumError) as exc:
                    raise PdfRenderError(f"PDF page {page_number} could not be rendered.") from exc
                finally:
                    if page is not None:
                        page.close()
            return tuple(rendered)

    def _expected_pixel_size(self, page: object, page_number: int) -> tuple[int, int]:
        width_points, height_points = page.get_size()  # type: ignore[attr-defined]
        if not all(math.isfinite(value) and value > 0 for value in (width_points, height_points)):
            raise PdfRenderError(f"PDF page {page_number} has invalid dimensions.")
        width = math.ceil(width_points * self.config.scale)
        height = math.ceil(height_points * self.config.scale)
        self._enforce_pixel_limit(page_number, width, height)
        return width, height

    def _render_page(
        self,
        page: object,
        page_number: int,
        expected_size: tuple[int, int],
        output_dir: Path,
    ) -> RenderedPage:
        bitmap = page.render(  # type: ignore[attr-defined]
            scale=self.config.scale,
            rotation=0,
            may_draw_forms=False,
            draw_annots=False,
            fill_color=(255, 255, 255, 255),
            optimize_mode="print",
            limit_image_cache=True,
        )
        try:
            image = bitmap.to_pil().convert("RGB")
            with image:
                if image.size != expected_size:
                    self._enforce_pixel_limit(page_number, *image.size)
                image_path = output_dir / f"page-{page_number:04d}.png"
                with image_path.open("xb") as output:
                    image.save(
                        output,
                        format="PNG",
                        compress_level=9,
                        optimize=False,
                        dpi=(self.config.dpi, self.config.dpi),
                    )
                    output.flush()
                    os.fsync(output.fileno())
        finally:
            bitmap.close()
        digest, size = hash_file(image_path, self.config.copy_chunk_bytes)
        return RenderedPage(
            page_number=page_number,
            image_path=f"page_images/page-{page_number:04d}.png",
            width_px=image.size[0],
            height_px=image.size[1],
            image_size_bytes=size,
            image_sha256=digest,
        )

    def _enforce_pixel_limit(self, page_number: int, width: int, height: int) -> None:
        if width * height > self.config.max_page_pixels:
            raise RenderedPagePixelLimitError(
                f"PDF page {page_number} exceeds the {self.config.max_page_pixels}-pixel limit."
            )

    def _build_manifest(
        self,
        stored: StoredPdf,
        pages: tuple[RenderedPage, ...],
    ) -> RenderManifest:
        return RenderManifest.build(
            schema_version=1,
            renderer="pypdfium2",
            source_path="source/original.pdf",
            document_id=stored.document_id,
            source_size_bytes=stored.source_size_bytes,
            configuration_fingerprint=self.config.fingerprint,
            dpi=self.config.dpi,
            color_mode="RGB",
            image_format="PNG",
            page_count=len(pages),
            pages=tuple(page.model_dump(mode="json") for page in pages),
        )

    def _write_manifest(self, path: Path, manifest: RenderManifest) -> None:
        encoded = (
            json.dumps(
                manifest.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        with path.open("xb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
