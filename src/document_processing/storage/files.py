"""Safe, durable filesystem primitives used by the storage repository."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from .models import IntegrityError, RunPaths

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def ensure_repository_root(root: Path, database_path: Path) -> None:
    """Create only real, contained repository directories and reject link redirection."""

    _ensure_real_directory(root)
    _ensure_real_directory(root / "runs", parent=root)
    _validate_database_paths(database_path)
    fsync_directory(root)
    fsync_directory(root.parent)


def validate_repository_root(root: Path, database_path: Path) -> None:
    _require_real_directory(root)
    _require_real_directory(root / "runs", parent=root)
    _validate_database_paths(database_path)


def _ensure_real_directory(path: Path, *, parent: Path | None = None) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise IntegrityError(f"storage path is not a real directory: {path}")
    path.mkdir(parents=parent is None, exist_ok=True)
    _require_real_directory(path, parent=parent)


def _require_real_directory(path: Path, *, parent: Path | None = None) -> None:
    if path.is_symlink() or not path.is_dir():
        raise IntegrityError(f"storage path is not a real directory: {path}")
    if parent is not None and path.resolve().parent != parent.resolve():
        raise IntegrityError(f"storage path escapes its parent: {path}")


def _validate_database_paths(database_path: Path) -> None:
    for candidate in (
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
    ):
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
            raise IntegrityError(f"database path is unsafe: {candidate}")


def validate_run_id(run_id: str) -> str:
    if run_id in {".", ".."} or not _RUN_ID.fullmatch(run_id):
        raise ValueError("run_id must be a safe 1-128 character identifier")
    return run_id


def paths_for(root: Path, run_id: str) -> RunPaths:
    validate_run_id(run_id)
    validate_repository_root(root, root / "state.sqlite3")
    run_root = root / "runs" / run_id
    if run_root.is_symlink() or (run_root.exists() and not run_root.is_dir()):
        raise IntegrityError(f"run path is unsafe: {run_root}")
    paths = RunPaths(
        root=run_root,
        source_dir=run_root / "source",
        page_images_dir=run_root / "page_images",
        initial_dir=run_root / "initial",
        page_artifacts_dir=run_root / "page_artifacts",
        staging_dir=run_root / ".staging",
        quarantine_dir=run_root / "quarantine",
        final_dir=run_root / "final",
    )
    if run_root.exists():
        validate_run_directories(paths)
    return paths


def ensure_run_directories(paths: RunPaths) -> None:
    _ensure_real_directory(paths.root, parent=paths.root.parent)
    for path in (
        paths.source_dir,
        paths.page_images_dir,
        paths.page_artifacts_dir,
        paths.staging_dir,
        paths.quarantine_dir,
    ):
        _ensure_real_directory(path, parent=paths.root)
    fsync_directory(paths.root)
    fsync_directory(paths.root.parent)


def validate_run_directories(paths: RunPaths) -> None:
    """Reject post-creation replacement of any artifact parent with a link."""

    _require_real_directory(paths.root, parent=paths.root.parent)
    for path in (
        paths.source_dir,
        paths.page_images_dir,
        paths.page_artifacts_dir,
        paths.staging_dir,
        paths.quarantine_dir,
    ):
        _require_real_directory(path, parent=paths.root)
    for optional in (paths.initial_dir, paths.final_dir):
        if optional.is_symlink() or (optional.exists() and not optional.is_dir()):
            raise IntegrityError(f"storage path is not a real directory: {optional}")
        if optional.exists():
            _require_real_directory(optional, parent=paths.root)


def jsonable(value: Any) -> Any:
    """Adapt Pydantic contracts without importing a particular contracts module."""
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json", by_alias=True)
    return value


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(
            jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def configuration_fingerprint(value: Any) -> str:
    """Match Settings' compact, sorted, UTF-8 fingerprint algorithm."""

    encoded = json.dumps(
        jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256_bytes(encoded)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_durable(path: Path, data: bytes, *, exclusive: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(str(path), flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        write_durable(temporary, data)
        os.replace(str(temporary), str(path))
        fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def durable_copy(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.replace(str(temporary), str(destination))
        fsync_directory(destination.parent)
        return sha256_file(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def validate_relative_path(value: str) -> PurePosixPath:
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or not parsed.parts or ".." in parsed.parts:
        raise ValueError(f"unsafe artifact path: {value!r}")
    return parsed


def resolve_artifact(run_root: Path, relative: str) -> Path:
    parsed = validate_relative_path(relative)
    candidate = run_root.joinpath(*parsed.parts)
    current = run_root
    for part in parsed.parts:
        current = current / part
        if current.is_symlink():
            raise IntegrityError(f"artifact path traverses a symlink: {relative}")
    return candidate


def relative_to_run(path: Path, run_root: Path) -> str:
    return path.relative_to(run_root).as_posix()


def quarantine(path: Path, quarantine_dir: Path, label: str) -> Path:
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    destination = quarantine_dir / f"{label}-{uuid.uuid4().hex}"
    os.rename(str(path), str(destination))
    fsync_directory(path.parent)
    fsync_directory(quarantine_dir)
    return destination
