"""Artifact-root initialization ownership tests for composed services."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from document_processing.analysis import FakeAnalyzer
from document_processing.composition import create_processor, create_service
from document_processing.config import Settings
from document_processing.errors import ArtifactRootLockedError
from document_processing.service.lock import ArtifactRootLock
from document_processing.storage import RunRepository


def install_fake_analyzer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "document_processing.composition.build_deep_agent_analyzer",
        lambda **_kwargs: FakeAnalyzer([]),
    )


def test_create_service_is_side_effect_free_for_artifact_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_analyzer(monkeypatch)
    settings = Settings(artifact_root=tmp_path / "artifacts")

    create_service(settings)

    assert not settings.artifact_root.exists()


def test_service_initializes_once_only_while_holding_root_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_analyzer(monkeypatch)
    original_initialize = RunRepository.initialize
    calls: list[tuple[Path, bool]] = []

    def observed_initialize(repository: RunRepository) -> None:
        contender = ArtifactRootLock(repository.root)
        try:
            contender.acquire()
        except ArtifactRootLockedError:
            lock_held = True
        else:
            lock_held = False
            contender.release()
        calls.append((repository.root, lock_held))
        original_initialize(repository)

    monkeypatch.setattr(RunRepository, "initialize", observed_initialize)
    settings = Settings(artifact_root=tmp_path / "artifacts")
    service = create_service(settings)
    assert calls == []

    async def scenario() -> None:
        await service.start()
        await service.start()
        await service.close()

    asyncio.run(scenario())

    assert calls == [(settings.artifact_root, True)]
    assert (settings.artifact_root / "state.sqlite3").is_file()


def test_losing_service_never_initializes_or_touches_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_analyzer(monkeypatch)
    original_initialize = RunRepository.initialize
    calls: list[Path] = []

    def counted_initialize(repository: RunRepository) -> None:
        calls.append(repository.root)
        original_initialize(repository)

    monkeypatch.setattr(RunRepository, "initialize", counted_initialize)
    settings = Settings(artifact_root=tmp_path / "artifacts")
    owner = create_service(settings)
    loser = create_service(settings)
    assert calls == []

    async def scenario() -> None:
        await owner.start()
        database = settings.artifact_root / "state.sqlite3"
        before = database.stat()
        await loser.start()
        assert not (await loser.readiness()).ready
        after = database.stat()
        assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
        await loser.close()
        await owner.close()

    asyncio.run(scenario())

    assert calls == [settings.artifact_root]


def test_standalone_processor_still_initializes_storage_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_initialize = RunRepository.initialize
    calls: list[Path] = []

    def counted_initialize(repository: RunRepository) -> None:
        calls.append(repository.root)
        original_initialize(repository)

    monkeypatch.setattr(RunRepository, "initialize", counted_initialize)
    settings = Settings(artifact_root=tmp_path / "processor-artifacts")

    create_processor(settings, analyzer=FakeAnalyzer([]))

    assert calls == [settings.artifact_root]
    assert (settings.artifact_root / "state.sqlite3").is_file()
