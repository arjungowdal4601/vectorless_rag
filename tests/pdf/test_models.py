"""Contract tests for strict render configuration and manifests."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from document_processing.pdf import PdfRenderConfig, RenderManifest
from document_processing.pdf.models import sha256_json

SHA_A = "a" * 64
SHA_B = "b" * 64


def manifest_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "renderer": "pypdfium2",
        "source_path": "source/original.pdf",
        "document_id": SHA_A,
        "source_size_bytes": 100,
        "configuration_fingerprint": SHA_B,
        "dpi": 200,
        "color_mode": "RGB",
        "image_format": "PNG",
        "page_count": 1,
        "pages": (
            {
                "page_number": 1,
                "image_path": "page_images/page-0001.png",
                "width_px": 100,
                "height_px": 200,
                "image_size_bytes": 50,
                "image_sha256": SHA_B,
            },
        ),
    }


def test_render_config_defaults_and_fingerprint_are_stable() -> None:
    config = PdfRenderConfig()

    assert config.dpi == 200
    assert config.max_upload_bytes == 100 * 1024 * 1024
    assert config.max_pages == 1_000
    assert config.max_page_pixels == 40_000_000
    assert config.fingerprint == sha256_json(config.model_dump(mode="json"))


def test_render_config_is_strict_and_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        PdfRenderConfig.model_validate({"dpi": "200"})
    with pytest.raises(ValidationError):
        PdfRenderConfig.model_validate({"unknown": 1})


def test_manifest_builds_and_validates_its_own_hash() -> None:
    manifest = RenderManifest.build(**manifest_payload())

    assert manifest.page_count == 1
    assert manifest.pages[0].page_number == 1
    assert manifest.manifest_sha256 == sha256_json(
        manifest.model_dump(mode="json", exclude={"manifest_sha256"})
    )


def test_manifest_rejects_hash_tampering_and_unknown_fields() -> None:
    manifest = RenderManifest.build(**manifest_payload())
    tampered = manifest.model_dump(mode="json")
    tampered["dpi"] = 201
    with pytest.raises(ValidationError, match="manifest_sha256"):
        RenderManifest.model_validate_json(json.dumps(tampered))

    unknown = manifest.model_dump(mode="json")
    unknown["extra"] = True
    with pytest.raises(ValidationError):
        RenderManifest.model_validate_json(json.dumps(unknown))


def test_manifest_rejects_non_contiguous_pages_and_unstable_paths() -> None:
    payload = manifest_payload()
    payload["pages"][0]["page_number"] = 2
    with pytest.raises(ValidationError):
        RenderManifest.build(**payload)

    payload = manifest_payload()
    payload["pages"][0]["image_path"] = "page_images/1.png"
    with pytest.raises(ValidationError):
        RenderManifest.build(**payload)


def test_manifest_rejects_page_count_mismatch() -> None:
    payload = manifest_payload()
    payload["page_count"] = 2

    with pytest.raises(ValidationError, match="page_count"):
        RenderManifest.build(**payload)
