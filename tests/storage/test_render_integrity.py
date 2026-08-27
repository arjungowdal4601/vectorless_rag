from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from PIL import Image

from document_processing.pdf.errors import RenderIntegrityError
from document_processing.pdf.models import PdfRenderConfig, RenderManifest, sha256_json
from document_processing.storage import RunRepository
from tests.pdf.helpers import PageSpec, make_pdf


class RenderIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _prepared_run(
        self,
        name: str,
        *,
        mode: str = "RGB",
        image_name: str = "page-0001.png",
        source_pages: int = 1,
    ) -> tuple[RunRepository, dict[str, Any]]:
        repository = RunRepository(self.root / name / "artifacts")
        repository.initialize()
        repository.create_run(name, {"model": "test:model"})
        source = self.root / name / "input.pdf"
        source.parent.mkdir(parents=True, exist_ok=True)
        make_pdf(source, tuple(PageSpec() for _ in range(source_pages)))
        run = repository.store_source(name, source)
        paths = repository.paths_for_run(name)
        image = paths.page_images_dir / image_name
        color = (1, 2, 3, 4) if mode == "RGBA" else (1, 2, 3)
        Image.new(mode, (200, 100), color).save(image, format="PNG")
        page = {
            "page_number": 1,
            "image_path": f"page_images/{image_name}",
            "width_px": 200,
            "height_px": 100,
            "image_size_bytes": image.stat().st_size,
            "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        }
        payload = {
            "schema_version": 1,
            "renderer": "pypdfium2",
            "source_path": "source/original.pdf",
            "document_id": run["source_sha256"],
            "source_size_bytes": paths.source_pdf.stat().st_size,
            "configuration_fingerprint": PdfRenderConfig().fingerprint,
            "dpi": 200,
            "color_mode": "RGB",
            "image_format": "PNG",
            "page_count": 1,
            "pages": [page],
        }
        return repository, payload

    def test_minimal_alias_manifest_is_rejected(self) -> None:
        repository, payload = self._prepared_run("minimal")
        minimal = {
            "document_id": payload["document_id"],
            "page_count": 1,
            "pages": [{"page_number": 1, "path": "page_images/page-0001.png"}],
        }

        with self.assertRaises(ValueError):
            repository.set_render_manifest("minimal", minimal)

    def test_non_stable_image_name_is_rejected(self) -> None:
        repository, payload = self._prepared_run("unstable", image_name="arbitrary.png")
        payload["manifest_sha256"] = sha256_json(payload)

        with self.assertRaises(ValueError):
            repository.set_render_manifest("unstable", payload)

    def test_wrong_render_configuration_is_rejected(self) -> None:
        repository, payload = self._prepared_run("configuration")
        payload["configuration_fingerprint"] = "0" * 64
        manifest = RenderManifest.build(**payload)

        with self.assertRaisesRegex(ValueError, "configuration fingerprint"):
            repository.set_render_manifest("configuration", manifest)

    def test_non_rgb_png_is_rejected(self) -> None:
        repository, payload = self._prepared_run("rgba", mode="RGBA")
        manifest = RenderManifest.build(**payload)

        with self.assertRaisesRegex(ValueError, "RGB PNG"):
            repository.set_render_manifest("rgba", manifest)

    def test_manifest_must_cover_every_source_pdf_page(self) -> None:
        repository, payload = self._prepared_run("missing-page", source_pages=2)
        manifest = RenderManifest.build(**payload)

        with self.assertRaisesRegex(RenderIntegrityError, "page count"):
            repository.set_render_manifest("missing-page", manifest)


if __name__ == "__main__":
    unittest.main()
