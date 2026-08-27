"""Cross-table validation for page-attempt ownership and ordinals."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from .database import Database


def validate_attempt_state(
    database: Database,
    run: Mapping[str, Any],
    pages: Sequence[Mapping[str, Any]],
) -> None:
    with database.connect() as connection:
        attempts = [
            dict(row)
            for row in connection.execute(
                """SELECT * FROM attempts WHERE run_id=?
                   ORDER BY page_number,ordinal""",
                (run["run_id"],),
            )
        ]
    by_page: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        by_page[int(attempt["page_number"])].append(attempt)
        running = attempt["status"] == "running"
        if running != (attempt["finished_at"] is None):
            raise ValueError("attempt status disagrees with its finished timestamp")

    running_pages = [page for page in pages if page["status"] == "running"]
    if len(running_pages) > 1:
        raise ValueError("more than one page owns a running attempt")
    for page in pages:
        page_number = int(page["page_number"])
        page_attempts = by_page.pop(page_number, [])
        attempt_count = int(page["attempt_count"])
        ordinals = [int(attempt["ordinal"]) for attempt in page_attempts]
        if ordinals != list(range(1, attempt_count + 1)):
            raise ValueError(f"page {page_number} attempt ordinals/count are inconsistent")
        running = [attempt for attempt in page_attempts if attempt["status"] == "running"]
        active_id = page["active_attempt_id"]
        if page["status"] == "running":
            if page_number != int(run["head_page"]) + 1:
                raise ValueError("running page is not the first incomplete page")
            if len(running) != 1 or active_id != running[0]["attempt_id"]:
                raise ValueError("running page does not own exactly one active attempt")
        elif active_id is not None or running:
            raise ValueError("non-running page retains an active/running attempt")
    if by_page:
        raise ValueError("attempt ledger references a missing page")
