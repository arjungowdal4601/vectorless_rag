"""Smoke tests for the loopback-only service entry point."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from document_processing import __main__ as cli
from document_processing.config import Settings


def test_main_uses_one_loopback_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in tuple(os.environ):
        if name.startswith(Settings.ENV_PREFIX):
            monkeypatch.delenv(name)
    monkeypatch.setenv(
        "DOCUMENT_PROCESSING_ARTIFACT_ROOT",
        str(tmp_path / "artifacts"),
    )

    sentinel = object()
    seen: dict[str, object] = {}

    def fake_create_application(settings: Settings | None = None) -> Any:
        seen["settings"] = settings
        return sentinel

    def fake_run(app: object, **kwargs: object) -> None:
        seen["app"] = app
        seen.update(kwargs)

    monkeypatch.setattr(cli, "create_application", fake_create_application)
    monkeypatch.setattr(uvicorn, "run", fake_run)

    cli.main()

    configured = seen["settings"]
    assert isinstance(configured, Settings)
    assert configured.host == "127.0.0.1"
    assert seen == {
        "settings": configured,
        "app": sentinel,
        "host": "127.0.0.1",
        "port": 8000,
        "access_log": True,
        "log_level": "info",
    }
