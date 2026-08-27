"""Rendered evidence paths must exactly match their assigned manifest pages."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from document_processing.processing.state import validate_prepared
from tests.processing.fakes import prepared_pdf


def test_reversed_page_paths_are_rejected(tmp_path: Path) -> None:
    prepared = prepared_pdf(tmp_path, 2)
    reversed_paths = replace(prepared, page_image_paths=tuple(reversed(prepared.page_image_paths)))

    with pytest.raises(ValueError, match="manifest order"):
        validate_prepared(reversed_paths, prepared.manifest_path.parent)


def test_outside_page_path_is_rejected(tmp_path: Path) -> None:
    prepared = prepared_pdf(tmp_path, 1)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")

    with pytest.raises(ValueError, match="manifest order"):
        validate_prepared(
            replace(prepared, page_image_paths=(outside,)), prepared.manifest_path.parent
        )


def test_symlinked_page_image_is_rejected(tmp_path: Path) -> None:
    prepared = prepared_pdf(tmp_path, 1)
    image = prepared.page_image_paths[0]
    target = tmp_path / "target.png"
    target.write_bytes(image.read_bytes())
    image.unlink()
    image.symlink_to(target)

    with pytest.raises(ValueError, match="symbolic link"):
        validate_prepared(prepared, prepared.manifest_path.parent)
