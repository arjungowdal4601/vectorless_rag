"""Tests for resume-time source, manifest, and image integrity audits."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from document_processing.pdf import (
    PdfIntakeService,
    PdfRenderConfig,
    RenderIntegrityError,
    RenderManifest,
)

from .helpers import PageSpec, make_pdf


def prepare_run(tmp_path: Path) -> tuple[PdfIntakeService, Path]:
    source_path = tmp_path / "fixture.pdf"
    make_pdf(source_path, (PageSpec(), PageSpec(width_points=36, height_points=36)))
    run_dir = tmp_path / "run"
    service = PdfIntakeService(PdfRenderConfig(dpi=72))
    service.prepare_path(source_path, run_dir)
    return service, run_dir


def test_verify_prepared_returns_ordered_artifacts(tmp_path: Path) -> None:
    service, run_dir = prepare_run(tmp_path)
    expected_id = (run_dir / "render_manifest.json").read_text("utf-8")
    document_id = json.loads(expected_id)["document_id"]

    prepared = service.verify_prepared(run_dir, document_id)

    assert prepared.document_id == document_id
    assert prepared.page_image_paths == (
        run_dir / "page_images/page-0001.png",
        run_dir / "page_images/page-0002.png",
    )


def test_verify_rejects_wrong_expected_document_id(tmp_path: Path) -> None:
    service, run_dir = prepare_run(tmp_path)

    with pytest.raises(RenderIntegrityError, match="identity"):
        service.verify_prepared(run_dir, "0" * 64)


def test_verify_rejects_tampered_source(tmp_path: Path) -> None:
    service, run_dir = prepare_run(tmp_path)
    with (run_dir / "source/original.pdf").open("ab") as source:
        source.write(b"tamper")

    with pytest.raises(RenderIntegrityError, match="source PDF failed"):
        service.verify_prepared(run_dir)


def test_verify_rejects_tampered_page_image(tmp_path: Path) -> None:
    service, run_dir = prepare_run(tmp_path)
    image_path = run_dir / "page_images/page-0001.png"
    image_path.write_bytes(image_path.read_bytes()[:-1] + b"x")

    with pytest.raises(RenderIntegrityError, match="page 1 failed"):
        service.verify_prepared(run_dir)


def test_verify_rejects_extra_or_missing_page_images(tmp_path: Path) -> None:
    service, run_dir = prepare_run(tmp_path)
    extra = run_dir / "page_images/unreferenced.png"
    extra.write_bytes(b"unexpected")

    with pytest.raises(RenderIntegrityError, match="image set"):
        service.verify_prepared(run_dir)

    extra.unlink()
    (run_dir / "page_images/page-0002.png").unlink()
    with pytest.raises(RenderIntegrityError, match="image set"):
        service.verify_prepared(run_dir)


def test_verify_rejects_manifest_content_tampering(tmp_path: Path) -> None:
    service, run_dir = prepare_run(tmp_path)
    manifest_path = run_dir / "render_manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["dpi"] = 73
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RenderIntegrityError, match="failed validation"):
        service.verify_prepared(run_dir)


def test_verify_rejects_changed_render_configuration(tmp_path: Path) -> None:
    _service, run_dir = prepare_run(tmp_path)
    changed_service = PdfIntakeService(PdfRenderConfig(dpi=100))

    with pytest.raises(RenderIntegrityError, match="fingerprint"):
        changed_service.verify_prepared(run_dir)


def test_verify_rejects_manifest_with_fewer_pages_than_source(tmp_path: Path) -> None:
    service, run_dir = prepare_run(tmp_path)
    manifest_path = run_dir / "render_manifest.json"
    payload = json.loads(manifest_path.read_text("utf-8"))
    payload.pop("manifest_sha256")
    payload["page_count"] = 1
    payload["pages"] = payload["pages"][:1]
    shortened = RenderManifest.build(**payload)
    manifest_path.write_text(shortened.model_dump_json(), encoding="utf-8")
    (run_dir / "page_images/page-0002.png").unlink()

    with pytest.raises(RenderIntegrityError, match="page count"):
        service.verify_prepared(run_dir)


def test_verify_rechecks_source_geometry_and_pixel_limit(tmp_path: Path) -> None:
    source_path = tmp_path / "large.pdf"
    make_pdf(source_path, (PageSpec(width_points=100, height_points=100),))
    run_dir = tmp_path / "run"
    initial = PdfIntakeService(PdfRenderConfig(dpi=72, max_page_pixels=20_000))
    prepared = initial.prepare_path(source_path, run_dir)
    strict_config = PdfRenderConfig(dpi=72, max_page_pixels=5_000)
    payload = prepared.manifest.model_dump(mode="json", exclude={"manifest_sha256"})
    payload["configuration_fingerprint"] = strict_config.fingerprint
    strict_manifest = RenderManifest.build(**payload)
    (run_dir / "render_manifest.json").write_text(
        strict_manifest.model_dump_json(), encoding="utf-8"
    )

    with pytest.raises(RenderIntegrityError, match="pixel limit"):
        PdfIntakeService(strict_config).verify_prepared(run_dir)


def test_verify_rejects_source_parent_symlink(tmp_path: Path) -> None:
    service, run_dir = prepare_run(tmp_path)
    source_dir = run_dir / "source"
    outside = tmp_path / "outside-source"
    source_dir.rename(outside)
    source_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RenderIntegrityError, match="unsafe"):
        service.verify_prepared(run_dir)


def test_verify_rejects_image_dimensions_that_disagree_with_source(tmp_path: Path) -> None:
    source_path = tmp_path / "fixture.pdf"
    make_pdf(source_path, (PageSpec(),))
    run_dir = tmp_path / "run"
    service = PdfIntakeService(PdfRenderConfig(dpi=72))
    prepared = service.prepare_path(source_path, run_dir)
    image_path = run_dir / "page_images/page-0001.png"
    with Image.new("RGB", (100, 100), "white") as replacement:
        replacement.save(image_path, format="PNG")
    payload = prepared.manifest.model_dump(mode="json", exclude={"manifest_sha256"})
    page = payload["pages"][0]
    page["width_px"] = 100
    page["height_px"] = 100
    page["image_size_bytes"] = image_path.stat().st_size
    page["image_sha256"] = hashlib.sha256(image_path.read_bytes()).hexdigest()
    rewritten = RenderManifest.build(**payload)
    (run_dir / "render_manifest.json").write_text(rewritten.model_dump_json(), encoding="utf-8")

    with pytest.raises(RenderIntegrityError, match="rendered dimensions"):
        service.verify_prepared(run_dir)
