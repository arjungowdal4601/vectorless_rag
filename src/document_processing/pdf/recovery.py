"""Explicit recovery actions for journal-proven unreferenced render artifacts."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from .artifact_io import fsync_directory
from .errors import (
    PdfArtifactConflictError,
    PdfStorageError,
    RenderIntegrityError,
)


def quarantine_unreferenced_render(run_dir: Path) -> Path | None:
    """Move orphaned images aside; callers must first prove they are unreferenced."""

    run_dir = Path(run_dir)
    image_target = run_dir / "page_images"
    manifest_target = run_dir / "render_manifest.json"
    if manifest_target.exists() or manifest_target.is_symlink():
        raise PdfArtifactConflictError("A render manifest still references this render set.")
    if not image_target.exists():
        return None
    if image_target.is_symlink() or not image_target.is_dir():
        raise RenderIntegrityError("The unreferenced page image path is unsafe.")
    if not any(image_target.iterdir()):
        return None
    quarantine_dir = run_dir / "quarantine"
    if quarantine_dir.is_symlink():
        raise RenderIntegrityError("The quarantine directory is unsafe.")
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    destination = quarantine_dir / f"unreferenced-render-{uuid.uuid4().hex}"
    try:
        os.replace(image_target, destination)
        fsync_directory(quarantine_dir)
        image_target.mkdir()
        fsync_directory(run_dir)
    except OSError as exc:
        raise PdfStorageError("The unreferenced render set could not be quarantined.") from exc
    return destination
