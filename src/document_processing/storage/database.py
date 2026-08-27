"""SQLite schema and transaction helpers for the local artifact repository."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN
        ('not_started','running','completed','failed','resuming')),
    phase TEXT NOT NULL,
    config_json TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    source_path TEXT,
    source_sha256 TEXT,
    original_filename TEXT,
    render_manifest_path TEXT,
    render_manifest_sha256 TEXT,
    total_pages INTEGER CHECK(total_pages IS NULL OR total_pages >= 1),
    head_page INTEGER NOT NULL DEFAULT 0 CHECK(head_page >= 0),
    head_commit_id TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
    failure_class TEXT,
    failure_code TEXT,
    failure_detail TEXT,
    final_manifest_path TEXT,
    final_manifest_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL CHECK(page_number >= 1),
    status TEXT NOT NULL CHECK(status IN
        ('pending','running','completed','failed','skipped')),
    image_path TEXT NOT NULL,
    image_sha256 TEXT NOT NULL,
    image_width INTEGER,
    image_height INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    active_attempt_id TEXT,
    commit_id TEXT,
    page_json_path TEXT,
    page_json_sha256 TEXT,
    error_class TEXT,
    error_code TEXT,
    error_detail TEXT,
    started_at TEXT,
    completed_at TEXT,
    PRIMARY KEY(run_id, page_number)
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN
        ('running','completed','failed','cancelled','interrupted')),
    model_id TEXT,
    request_fingerprint TEXT,
    response_id TEXT,
    usage_json TEXT,
    failure_class TEXT,
    failure_code TEXT,
    failure_detail TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(run_id, page_number, ordinal),
    FOREIGN KEY(run_id, page_number) REFERENCES pages(run_id, page_number)
);

CREATE TABLE IF NOT EXISTS commits (
    commit_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL CHECK(page_number >= 0),
    previous_commit_id TEXT,
    relative_path TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    page_json_sha256 TEXT,
    memory_sha256 TEXT NOT NULL,
    structure_sha256 TEXT NOT NULL,
    attempt_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, page_number),
    UNIQUE(run_id, relative_path)
);

CREATE TABLE IF NOT EXISTS recovery_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pages_run_status ON pages(run_id, status, page_number);
CREATE INDEX IF NOT EXISTS idx_attempts_run_page ON attempts(run_id, page_number, ordinal);
CREATE INDEX IF NOT EXISTS idx_commits_run_page ON commits(run_id, page_number);
CREATE INDEX IF NOT EXISTS idx_recovery_run ON recovery_events(run_id, event_id);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
