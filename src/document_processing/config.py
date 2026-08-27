"""Strict, secret-free application configuration."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from document_processing.errors import ConfigurationError

_INTEGER = re.compile(r"[0-9]+\Z")
_DECIMAL = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\Z")


class Settings(BaseModel):
    """Validated settings loaded from an explicit environment allowlist.

    API keys intentionally do not belong to this model. Provider clients read
    them from their environment, and neither fingerprints nor diagnostics can
    therefore disclose them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ENV_PREFIX: ClassVar[str] = "DOCUMENT_PROCESSING_"
    DEEP_AGENTS_VERSION: ClassVar[str] = "0.7.9"
    CONFIGURATION_REVISION: ClassVar[str] = "1"

    artifact_root: Path = Path("var/document-processing")
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    model: str = "gpt-5.6-luna"
    reasoning_effort: Literal["low", "medium", "high"] = "medium"
    render_dpi: int = Field(default=200, ge=72, le=600)
    max_upload_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    max_pages: int = Field(default=1_000, ge=1)
    max_rendered_megapixels: float = Field(default=40.0, gt=0)
    model_timeout_seconds: float = Field(default=180.0, gt=0)
    max_model_attempts: int = Field(default=3, ge=1, le=3)
    worker_queue_capacity: int = Field(default=100, ge=1, le=10_000)
    shutdown_grace_seconds: float = Field(default=30.0, ge=0, le=300)
    page_list_default_limit: int = Field(default=100, ge=1, le=1_000)
    page_list_max_limit: int = Field(default=500, ge=1, le=1_000)

    @field_validator("artifact_root")
    @classmethod
    def validate_artifact_root(cls, value: Path) -> Path:
        if not str(value) or "\x00" in str(value):
            raise ValueError("artifact_root must be a non-empty filesystem path")
        expanded = value.expanduser()
        if expanded == Path(expanded.anchor):
            raise ValueError("artifact_root cannot be a filesystem root")
        return expanded

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not value or value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("model must be a non-empty, trimmed identifier")
        return value

    @model_validator(mode="after")
    def validate_page_limits(self) -> Self:
        if self.page_list_default_limit > self.page_list_max_limit:
            raise ValueError("page_list_default_limit cannot exceed page_list_max_limit")
        return self

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Self:
        """Load only documented variables and reject misspelled prefixed keys."""

        source = os.environ if environ is None else environ
        parsers = cls._environment_parsers()
        prefixed = {key for key in source if key.startswith(cls.ENV_PREFIX)}
        unknown = sorted(prefixed - set(parsers))
        if unknown:
            joined = ", ".join(unknown)
            raise ConfigurationError(f"Unknown configuration variable(s): {joined}")

        values: dict[str, object] = {}
        try:
            for env_name, (field_name, parser) in parsers.items():
                if env_name in source:
                    values[field_name] = parser(source[env_name], env_name)
            return cls.model_validate(values, strict=True)
        except ConfigurationError:
            raise
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(str(exc)) from exc

    @classmethod
    def _environment_parsers(
        cls,
    ) -> dict[str, tuple[str, Callable[[str, str], object]]]:
        prefix = cls.ENV_PREFIX
        return {
            f"{prefix}ARTIFACT_ROOT": ("artifact_root", cls._parse_path),
            f"{prefix}HOST": ("host", cls._parse_string),
            f"{prefix}PORT": ("port", cls._parse_int),
            f"{prefix}MODEL": ("model", cls._parse_string),
            f"{prefix}REASONING_EFFORT": ("reasoning_effort", cls._parse_string),
            f"{prefix}RENDER_DPI": ("render_dpi", cls._parse_int),
            f"{prefix}MAX_UPLOAD_BYTES": ("max_upload_bytes", cls._parse_int),
            f"{prefix}MAX_PAGES": ("max_pages", cls._parse_int),
            f"{prefix}MAX_RENDERED_MEGAPIXELS": (
                "max_rendered_megapixels",
                cls._parse_float,
            ),
            f"{prefix}MODEL_TIMEOUT_SECONDS": ("model_timeout_seconds", cls._parse_float),
            f"{prefix}MAX_MODEL_ATTEMPTS": ("max_model_attempts", cls._parse_int),
            f"{prefix}WORKER_QUEUE_CAPACITY": ("worker_queue_capacity", cls._parse_int),
            f"{prefix}SHUTDOWN_GRACE_SECONDS": ("shutdown_grace_seconds", cls._parse_float),
            f"{prefix}PAGE_LIST_DEFAULT_LIMIT": ("page_list_default_limit", cls._parse_int),
            f"{prefix}PAGE_LIST_MAX_LIMIT": ("page_list_max_limit", cls._parse_int),
        }

    @staticmethod
    def _parse_string(raw: str, name: str) -> str:
        if not raw or raw != raw.strip() or "\x00" in raw:
            raise ConfigurationError(f"{name} must be non-empty and contain no outer whitespace")
        return raw

    @staticmethod
    def _parse_path(raw: str, name: str) -> Path:
        return Path(Settings._parse_string(raw, name))

    @staticmethod
    def _parse_int(raw: str, name: str) -> int:
        if not _INTEGER.fullmatch(raw):
            raise ConfigurationError(f"{name} must be an unsigned base-10 integer")
        return int(raw)

    @staticmethod
    def _parse_float(raw: str, name: str) -> float:
        if not _DECIMAL.fullmatch(raw):
            raise ConfigurationError(f"{name} must be a finite non-negative decimal")
        value = float(raw)
        if not math.isfinite(value):
            raise ConfigurationError(f"{name} must be a finite non-negative decimal")
        return value

    def fingerprint_payload(self) -> dict[str, object]:
        """Return the processing-affecting, secret-free configuration record."""

        return {
            "configuration_revision": self.CONFIGURATION_REVISION,
            "deepagents_version": self.DEEP_AGENTS_VERSION,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "render_dpi": self.render_dpi,
            "max_upload_bytes": self.max_upload_bytes,
            "max_pages": self.max_pages,
            "max_rendered_megapixels": self.max_rendered_megapixels,
            "model_timeout_seconds": self.model_timeout_seconds,
            "max_model_attempts": self.max_model_attempts,
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.fingerprint_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
