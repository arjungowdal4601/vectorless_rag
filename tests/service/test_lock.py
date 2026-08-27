"""Exclusive artifact-root ownership tests."""

from pathlib import Path

import pytest

from document_processing.errors import ArtifactRootLockedError
from document_processing.service.lock import ArtifactRootLock


def test_lock_is_exclusive_and_releasable(tmp_path: Path) -> None:
    first = ArtifactRootLock(tmp_path / "artifacts")
    second = ArtifactRootLock(tmp_path / "artifacts")

    first.acquire()
    assert first.is_held
    assert first.path.read_text(encoding="utf-8").startswith("pid=")
    with pytest.raises(ArtifactRootLockedError):
        second.acquire()

    first.release()
    second.acquire()
    assert second.is_held
    second.release()


def test_lock_context_is_idempotent(tmp_path: Path) -> None:
    lock = ArtifactRootLock(tmp_path / "artifacts")

    with lock:
        lock.acquire()
        assert lock.is_held

    assert not lock.is_held


def test_lock_rejects_symlinked_artifact_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(ArtifactRootLockedError):
        ArtifactRootLock(linked_root).acquire()


def test_lock_rejects_symlink_without_truncating_target(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("must remain intact", encoding="utf-8")
    (artifact_root / ".service.lock").symlink_to(victim)

    with pytest.raises(ArtifactRootLockedError):
        ArtifactRootLock(artifact_root).acquire()

    assert victim.read_text(encoding="utf-8") == "must remain intact"
