"""Preparation and verification of immutable per-page commit directories."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .files import (
    canonical_json,
    fsync_directory,
    relative_to_run,
    sha256_bytes,
    sha256_file,
    write_durable,
)
from .models import CommitRecord, FaultHook, IntegrityError, RunPaths


@dataclass(frozen=True)
class PreparedCommit:
    record: CommitRecord
    path: Path
    manifest: Mapping[str, Any]
    files: Mapping[str, str]


def prepare_commit(
    *,
    paths: RunPaths,
    run_id: str,
    page_number: int,
    previous_commit_id: str | None,
    config_sha256: str,
    source_sha256: str,
    image_path: str | None,
    image_sha256: str | None,
    short_term_memory: Any,
    document_structure: Any,
    page: Any | None = None,
    model_response: Any | None = None,
    usage: Any | None = None,
    attempt_id: str | None = None,
    fault_hook: FaultHook | None = None,
) -> PreparedCommit:
    token = uuid.uuid4().hex
    staging = paths.staging_dir / f"commit-{page_number:06d}-{token}"
    staging.mkdir(parents=False, exist_ok=False)
    payloads: dict[str, bytes] = {
        "short_term_memory.json": canonical_json(short_term_memory),
        "document_structure.json": canonical_json(document_structure),
    }
    if page_number > 0:
        if page is None or model_response is None:
            raise ValueError("page commits require page and model_response")
        payloads.update(
            {
                "page.json": canonical_json(page),
                "model_response.json": canonical_json(model_response),
                "usage.json": canonical_json(usage or {}),
            }
        )
    for name, data in payloads.items():
        write_durable(staging / name, data)
    file_hashes = {name: sha256_bytes(data) for name, data in payloads.items()}
    manifest = {
        "schema_version": 1,
        "kind": "initial" if page_number == 0 else "page",
        "run_id": run_id,
        "page_number": page_number,
        "previous_commit_id": previous_commit_id,
        "attempt_id": attempt_id,
        "config_sha256": config_sha256,
        "source_sha256": source_sha256,
        "image_path": image_path,
        "image_sha256": image_sha256,
        "files": file_hashes,
    }
    manifest_bytes = canonical_json(manifest)
    commit_id = sha256_bytes(manifest_bytes)
    write_durable(staging / "commit.json", manifest_bytes)
    fsync_directory(staging)
    if fault_hook:
        fault_hook("after_staging_fsync")
    final = (
        paths.initial_dir
        if page_number == 0
        else paths.page_artifacts_dir / f"page-{page_number:04d}"
    )
    if final.exists():
        raise IntegrityError(f"commit destination already exists: {final}")
    os.rename(str(staging), str(final))
    fsync_directory(final.parent)
    if fault_hook:
        fault_hook("after_commit_rename")
    relative = relative_to_run(final, paths.root)
    return PreparedCommit(
        record=CommitRecord(
            commit_id=commit_id,
            run_id=run_id,
            page_number=page_number,
            relative_path=relative,
            manifest_sha256=sha256_bytes(manifest_bytes),
            page_json_path=(f"{relative}/page.json" if page_number else None),
            short_term_memory_path=f"{relative}/short_term_memory.json",
            document_structure_path=f"{relative}/document_structure.json",
        ),
        path=final,
        manifest=manifest,
        files=file_hashes,
    )


def verify_commit(path: Path, expected_commit_id: str) -> Mapping[str, Any]:
    manifest_path = path / "commit.json"
    if not manifest_path.is_file():
        raise IntegrityError(f"missing commit manifest: {manifest_path}")
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IntegrityError(f"invalid commit manifest: {manifest_path}") from error
    if sha256_bytes(canonical_json(manifest)) != expected_commit_id:
        raise IntegrityError(f"commit id mismatch: {path}")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise IntegrityError(f"invalid commit file map: {path}")
    entries = list(path.iterdir())
    if any(entry.is_symlink() or not entry.is_file() for entry in entries):
        raise IntegrityError(f"unsafe commit directory entry: {path}")
    if {entry.name for entry in entries} != {"commit.json", *files}:
        raise IntegrityError(f"commit directory contents differ from manifest: {path}")
    for name, expected_hash in files.items():
        if Path(name).name != name:
            raise IntegrityError(f"unsafe commit filename: {name}")
        artifact = path / name
        if not artifact.is_file() or artifact.is_symlink():
            raise IntegrityError(f"missing or unsafe commit artifact: {artifact}")
        if sha256_file(artifact) != expected_hash:
            raise IntegrityError(f"commit artifact hash mismatch: {artifact}")
        if name.endswith(".json"):
            try:
                json.loads(artifact.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise IntegrityError(f"invalid JSON artifact: {artifact}") from error
    return cast(Mapping[str, Any], manifest)
