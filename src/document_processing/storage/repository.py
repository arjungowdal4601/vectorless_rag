"""Narrow facade for local durable document-processing state."""

from __future__ import annotations

from pathlib import Path

from .database import Database
from .finalization import FinalizationMixin


class RunRepository(FinalizationMixin):
    """Own SQLite state and immutable artifacts below one artifact root."""

    def __init__(self, artifact_root: Path) -> None:
        self.root = Path(artifact_root)
        self._db = Database(self.root / "state.sqlite3")
