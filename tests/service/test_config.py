"""Configuration parsing and fingerprint tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from document_processing.config import Settings
from document_processing.errors import ConfigurationError
from document_processing.storage.files import canonical_json, sha256_bytes


def test_defaults_are_loopback_and_requested_model() -> None:
    settings = Settings()

    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.model == "gpt-5.6-luna"
    assert settings.reasoning_effort == "medium"
    assert settings.render_dpi == 200
    assert settings.max_model_attempts == 3
    assert len(settings.fingerprint) == 64


def test_from_env_parses_only_allowlisted_exact_values(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "DOCUMENT_PROCESSING_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            "DOCUMENT_PROCESSING_PORT": "8123",
            "DOCUMENT_PROCESSING_MAX_UPLOAD_BYTES": "42",
            "DOCUMENT_PROCESSING_MAX_RENDERED_MEGAPIXELS": "12.5",
            "UNRELATED": "ignored",
        }
    )

    assert settings.artifact_root == tmp_path / "artifacts"
    assert settings.port == 8123
    assert settings.max_upload_bytes == 42
    assert settings.max_rendered_megapixels == 12.5


@pytest.mark.parametrize("raw", [" 8000", "+8000", "8_000", "8000.0", "-1"])
def test_from_env_rejects_ambiguous_integer_syntax(raw: str) -> None:
    with pytest.raises(ConfigurationError):
        Settings.from_env({"DOCUMENT_PROCESSING_PORT": raw})


def test_from_env_rejects_unknown_prefixed_variable() -> None:
    with pytest.raises(ConfigurationError, match="UNKNOWN_SETTING"):
        Settings.from_env({"DOCUMENT_PROCESSING_UNKNOWN_SETTING": "1"})


def test_settings_reject_non_loopback_host() -> None:
    with pytest.raises((ConfigurationError, ValidationError)):
        Settings.from_env({"DOCUMENT_PROCESSING_HOST": "0.0.0.0"})


def test_fingerprint_excludes_deployment_location_and_is_stable(tmp_path: Path) -> None:
    first = Settings(artifact_root=tmp_path / "one", port=8000)
    second = Settings(artifact_root=tmp_path / "two", port=9000)

    assert first.fingerprint == second.fingerprint
    assert first.fingerprint_payload()["deepagents_version"] == "0.7.9"
    assert "artifact_root" not in first.fingerprint_payload()


def test_fingerprint_changes_for_processing_setting(tmp_path: Path) -> None:
    first = Settings(artifact_root=tmp_path / "one")
    second = Settings(artifact_root=tmp_path / "one", render_dpi=300)

    assert first.fingerprint != second.fingerprint


def test_fingerprint_matches_the_durable_journal_for_unicode_configuration() -> None:
    settings = Settings(model="gpt-model-β")
    durable_bytes = canonical_json(settings.fingerprint_payload()).removesuffix(b"\n")

    assert settings.fingerprint == sha256_bytes(durable_bytes)


def test_model_attempt_budget_cannot_exceed_three() -> None:
    with pytest.raises(ValidationError):
        Settings(max_model_attempts=4)


def test_page_list_default_cannot_exceed_maximum() -> None:
    with pytest.raises(ValidationError):
        Settings(page_list_default_limit=10, page_list_max_limit=9)
