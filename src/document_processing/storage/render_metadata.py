"""Validation of rendered-page metadata before it enters SQLite."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from PIL import Image

from document_processing.pdf.models import (
    PdfRenderConfig,
    RenderedPage,
    RenderManifest,
    canonical_json_bytes,
)
from document_processing.pdf.verifier import _verify_source_pages

from .files import jsonable, relative_to_run, resolve_artifact, sha256_file
from .models import RunPaths

RenderRow = tuple[int, str, str, int, int]


def validate_render_manifest(
    paths: RunPaths,
    raw: Any,
    run: Mapping[str, Any],
) -> tuple[RenderManifest, list[RenderRow]]:
    """Strictly bind a self-checking render manifest to its run and artifacts."""

    if isinstance(raw, RenderManifest):
        manifest = raw
    elif isinstance(raw, (bytes, str)):
        manifest = RenderManifest.model_validate_json(raw)
    else:
        manifest = RenderManifest.model_validate_json(canonical_json_bytes(jsonable(raw)))
    source = resolve_artifact(paths.root, manifest.source_path)
    if manifest.document_id != run.get("source_sha256"):
        raise ValueError("render manifest does not bind the preserved source")
    if not source.is_file() or source.is_symlink():
        raise ValueError("preserved source is missing or unsafe")
    if manifest.source_size_bytes != source.stat().st_size:
        raise ValueError("render manifest source size mismatch")
    render_config = render_config_for(json.loads(run["config_json"]))
    if manifest.dpi != render_config.dpi:
        raise ValueError("render manifest DPI differs from the run configuration")
    if manifest.configuration_fingerprint != render_config.fingerprint:
        raise ValueError("render configuration fingerprint mismatch")
    _verify_source_pages(source, manifest, render_config)
    rows = validate_render_pages(paths, manifest.pages, manifest.page_count, render_config)
    return manifest, rows


def render_config_for(configuration: Mapping[str, Any]) -> PdfRenderConfig:
    """Derive the renderer's public, fingerprinted settings from run configuration."""

    megapixels = configuration.get("max_rendered_megapixels", 40.0)
    return PdfRenderConfig(
        dpi=configuration.get("render_dpi", 200),
        max_upload_bytes=configuration.get("max_upload_bytes", 100 * 1024 * 1024),
        max_pages=configuration.get("max_pages", 1_000),
        max_page_pixels=int(float(megapixels) * 1_000_000),
    )


def validate_render_pages(
    paths: RunPaths,
    pages: Sequence[RenderedPage],
    count: int,
    config: PdfRenderConfig,
) -> list[RenderRow]:
    rows: list[RenderRow] = []
    for expected, item in enumerate(pages, start=1):
        if item.page_number != expected:
            raise ValueError("rendered pages must be contiguous")
        artifact = resolve_artifact(paths.root, item.image_path)
        if (
            artifact.is_symlink()
            or not artifact.is_file()
            or artifact.parent != paths.page_images_dir
        ):
            raise ValueError(f"missing or unsafe page image: {item.image_path}")
        actual_hash = sha256_file(artifact)
        if item.image_sha256 != actual_hash or item.image_size_bytes != artifact.stat().st_size:
            raise ValueError(f"page image hash/size mismatch: {item.image_path}")
        if item.width_px * item.height_px > config.max_page_pixels:
            raise ValueError(f"page image exceeds configured pixel limit: {item.image_path}")
        with Image.open(artifact) as image:
            if image.format != "PNG" or image.mode != "RGB":
                raise ValueError(f"page image must be an RGB PNG: {item.image_path}")
            if image.size != (item.width_px, item.height_px):
                raise ValueError(f"page image dimensions changed: {item.image_path}")
            image.verify()
        rows.append(
            (
                item.page_number,
                item.image_path,
                actual_hash,
                item.width_px,
                item.height_px,
            )
        )
    if len(rows) != count:
        raise ValueError("rendered page count mismatch")
    validate_exact_render_set(paths.root, pages)
    return rows


def validate_exact_render_set(run_root: Path, pages: Sequence[Any]) -> None:
    """Require page_images to contain exactly the manifest's regular files."""

    expected: list[str] = []
    for raw in pages:
        item = jsonable(raw)
        relative = item.get("image_path", item.get("path", item.get("page_image_path")))
        if not isinstance(relative, str):
            raise ValueError("render manifest contains an invalid image path")
        expected.append(relative)
    if len(expected) != len(set(expected)):
        raise ValueError("render manifest image paths must be unique")
    image_dir = run_root / "page_images"
    if image_dir.is_symlink() or not image_dir.is_dir():
        raise ValueError("page_images must be a regular directory")
    actual: list[str] = []
    for artifact in image_dir.iterdir():
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError(f"unsafe or unexpected page image entry: {artifact.name}")
        actual.append(relative_to_run(artifact, run_root))
    if set(actual) != set(expected):
        raise ValueError("page_images contents do not exactly match the render manifest")
