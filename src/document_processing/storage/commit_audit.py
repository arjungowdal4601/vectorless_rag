"""Cross-ledger validation for immutable commits and model attempts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from document_processing.contracts import ModelUsageRecord

from .database import Database
from .files import canonical_json

_MANIFEST_FIELDS = {
    "schema_version",
    "kind",
    "run_id",
    "page_number",
    "previous_commit_id",
    "attempt_id",
    "config_sha256",
    "source_sha256",
    "image_path",
    "image_sha256",
    "files",
}
_INITIAL_FILES = {"short_term_memory.json", "document_structure.json"}
_PAGE_FILES = _INITIAL_FILES | {"page.json", "model_response.json", "usage.json"}


def completed_attempt_index(
    database: Database,
    run_id: str,
    commits: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    with database.connect() as connection:
        attempts = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM attempts WHERE run_id=? ORDER BY page_number,ordinal",
                (run_id,),
            )
        ]
    expected = [row["attempt_id"] for row in commits if row["page_number"] > 0]
    if any(attempt_id is None for attempt_id in expected) or len(expected) != len(set(expected)):
        raise ValueError("page commits must reference distinct attempts")
    completed: dict[str, Mapping[str, Any]] = {
        row["attempt_id"]: row for row in attempts if row["status"] == "completed"
    }
    if set(expected) != set(completed):
        raise ValueError("completed attempts do not correspond exactly to page commits")
    return completed


def validate_attempt_link(
    attempts: Mapping[str, Mapping[str, Any]],
    commit: Mapping[str, Any],
    *,
    page_number: int,
    usage_bytes: bytes,
) -> None:
    attempt_id = commit["attempt_id"]
    attempt = attempts.get(attempt_id)
    if attempt is None:
        raise ValueError("page commit has no completed attempt")
    if (
        attempt["run_id"] != commit["run_id"]
        or attempt["page_number"] != page_number
        or attempt["status"] != "completed"
        or attempt["finished_at"] is None
    ):
        raise ValueError("page commit attempt identity/status mismatch")
    if any(
        attempt[field] is not None for field in ("failure_class", "failure_code", "failure_detail")
    ):
        raise ValueError("completed page attempt retains failure metadata")
    stored_usage = ModelUsageRecord.model_validate_json(usage_bytes)
    if attempt["usage_json"] is None:
        raise ValueError("completed page attempt has no usage projection")
    ledger_usage = ModelUsageRecord.model_validate_json(attempt["usage_json"])
    if canonical_json(stored_usage) != canonical_json(ledger_usage):
        raise ValueError("attempt usage differs from the immutable commit")
    if stored_usage.attempt_number != attempt["ordinal"]:
        raise ValueError("attempt ordinal differs from committed usage")
    if attempt["model_id"] and stored_usage.model_id != attempt["model_id"]:
        raise ValueError("attempt model differs from committed usage")


def validate_commit_metadata(
    run: Mapping[str, Any],
    row: Mapping[str, Any],
    manifest: Mapping[str, Any],
    previous: str | None,
) -> None:
    page_number = row["page_number"]
    initial = page_number == 0
    if set(manifest) != _MANIFEST_FIELDS:
        raise ValueError("commit manifest fields are not exact")
    if manifest["schema_version"] != 1:
        raise ValueError("commit manifest schema version mismatch")
    if manifest["kind"] != ("initial" if initial else "page"):
        raise ValueError("commit manifest kind mismatch")
    files = manifest["files"]
    if not isinstance(files, Mapping) or set(files) != (_INITIAL_FILES if initial else _PAGE_FILES):
        raise ValueError("commit manifest file set mismatch")
    expected = {
        "run_id": run["run_id"],
        "page_number": page_number,
        "previous_commit_id": previous,
        "config_sha256": run["config_sha256"],
        "source_sha256": run["source_sha256"],
        "attempt_id": row["attempt_id"],
    }
    if any(manifest[key] != value for key, value in expected.items()):
        raise ValueError("commit manifest metadata mismatch")
    if row["previous_commit_id"] != previous:
        raise ValueError("commit ledger predecessor mismatch")
    if row["manifest_sha256"] != row["commit_id"]:
        raise ValueError("commit ledger manifest hash mismatch")
    if row["memory_sha256"] != files["short_term_memory.json"]:
        raise ValueError("commit ledger memory hash mismatch")
    if row["structure_sha256"] != files["document_structure.json"]:
        raise ValueError("commit ledger structure hash mismatch")
    if row["page_json_sha256"] != files.get("page.json"):
        raise ValueError("commit ledger page JSON hash mismatch")
    page_metadata = ("attempt_id", "image_path", "image_sha256")
    if initial and any(manifest[key] is not None for key in page_metadata):
        raise ValueError("initial commit contains page-only metadata")
    if not initial and any(manifest[key] is None for key in page_metadata):
        raise ValueError("page commit is missing required metadata")
