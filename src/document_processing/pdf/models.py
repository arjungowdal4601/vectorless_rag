"""Strict contracts for preserved PDFs and rendered page artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SHA256_PATTERN = r"^[0-9a-f]{64}$"


def canonical_json_bytes(value: Any) -> bytes:
    """Return the stable JSON representation used for artifact hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    """Hash a JSON-compatible value using the canonical representation."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class StrictModel(BaseModel):
    """Base class that rejects unknown fields and type coercion."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class PdfRenderConfig(StrictModel):
    """Fingerprintable PDF intake and rasterization limits."""

    dpi: Annotated[int, Field(ge=36, le=1200)] = 200
    max_upload_bytes: Annotated[int, Field(ge=1)] = 100 * 1024 * 1024
    max_pages: Annotated[int, Field(ge=1)] = 1_000
    max_page_pixels: Annotated[int, Field(ge=1)] = 40_000_000
    copy_chunk_bytes: Annotated[int, Field(ge=4_096, le=16 * 1024 * 1024)] = 1024 * 1024

    @property
    def scale(self) -> float:
        """PDFium scale corresponding to the configured dots per inch."""

        return self.dpi / 72.0

    @property
    def fingerprint(self) -> str:
        """Stable hash embedded into every render manifest."""

        return sha256_json(self.model_dump(mode="json"))


class RenderedPage(StrictModel):
    """Manifest entry for one one-based, ordered page image."""

    page_number: Annotated[int, Field(ge=1)]
    image_path: str
    width_px: Annotated[int, Field(ge=1)]
    height_px: Annotated[int, Field(ge=1)]
    image_size_bytes: Annotated[int, Field(ge=1)]
    image_sha256: Annotated[str, Field(pattern=SHA256_PATTERN)]

    @model_validator(mode="after")
    def validate_stable_path(self) -> Self:
        expected = f"page_images/page-{self.page_number:04d}.png"
        if self.image_path != expected:
            raise ValueError(f"image_path must be {expected!r}")
        return self


class RenderManifest(StrictModel):
    """Self-checking manifest for the source and complete rendered page set."""

    schema_version: Literal[1]
    renderer: Literal["pypdfium2"]
    source_path: Literal["source/original.pdf"]
    document_id: Annotated[str, Field(pattern=SHA256_PATTERN)]
    source_size_bytes: Annotated[int, Field(ge=1)]
    configuration_fingerprint: Annotated[str, Field(pattern=SHA256_PATTERN)]
    dpi: Annotated[int, Field(ge=36, le=1200)]
    color_mode: Literal["RGB"]
    image_format: Literal["PNG"]
    page_count: Annotated[int, Field(ge=1)]
    pages: tuple[RenderedPage, ...]
    manifest_sha256: Annotated[str, Field(pattern=SHA256_PATTERN)]

    @classmethod
    def build(cls, **payload: Any) -> Self:
        """Create a manifest and bind its hash to every non-hash field."""

        digest = sha256_json(payload)
        candidate = {**payload, "manifest_sha256": digest}
        return cls.model_validate_json(canonical_json_bytes(candidate))

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if self.page_count != len(self.pages):
            raise ValueError("page_count must equal the number of page entries")
        expected_numbers = tuple(range(1, self.page_count + 1))
        if tuple(page.page_number for page in self.pages) != expected_numbers:
            raise ValueError("pages must form a contiguous one-based sequence")
        payload = self.model_dump(mode="json", exclude={"manifest_sha256"})
        if sha256_json(payload) != self.manifest_sha256:
            raise ValueError("manifest_sha256 does not match manifest content")
        return self


@dataclass(frozen=True, slots=True)
class StoredPdf:
    """Metadata for an atomically preserved source PDF."""

    document_id: str
    source_path: Path
    source_size_bytes: int


@dataclass(frozen=True, slots=True)
class PreparedPdf:
    """Ordered artifacts ready for strictly sequential page analysis."""

    document_id: str
    source_path: Path
    source_size_bytes: int
    manifest_path: Path
    manifest: RenderManifest
    page_image_paths: tuple[Path, ...]
