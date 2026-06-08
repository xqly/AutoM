from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from .config import PROJECT_ROOT, Settings


def connect(settings: Settings) -> sqlite3.Connection:
    conn = sqlite3.connect(settings.database_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    for name in ("attachments", "jobs", "artifacts"):
        (settings.data_dir / name).mkdir(parents=True, exist_ok=True)
    schema_path = PROJECT_ROOT / "schema.sql"
    with connect(settings) as conn:
        conn.executescript(schema_path.read_text(encoding="utf-8"))
        migrate_artifacts_kind_check(conn)


def migrate_artifacts_kind_check(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'artifacts'"
    ).fetchone()
    create_sql = str(row["sql"] if row is not None else "")
    if "maycad_plan" in create_sql:
        return
    conn.executescript(
        """
        ALTER TABLE artifacts RENAME TO artifacts_old;

        CREATE TABLE artifacts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          request_id INTEGER NOT NULL REFERENCES drawing_requests(id) ON DELETE CASCADE,
          job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
          kind TEXT NOT NULL CHECK (
            kind IN (
              'maycad_plan','bom_csv','cut_list_csv',
              'madcad_script','stl',
              'preview_png','manifest','log','final_json'
            )
          ),
          storage_path TEXT NOT NULL,
          original_name TEXT NOT NULL,
          mime_type TEXT,
          size_bytes INTEGER NOT NULL,
          sha256 TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        INSERT INTO artifacts
          (id, request_id, job_id, kind, storage_path, original_name, mime_type, size_bytes, sha256, created_at)
        SELECT id, request_id, job_id, kind, storage_path, original_name, mime_type, size_bytes, sha256, created_at
        FROM artifacts_old;

        DROP TABLE artifacts_old;
        CREATE INDEX IF NOT EXISTS idx_artifacts_request_kind ON artifacts(request_id, kind);
        """
    )


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [row_to_dict(row) for row in rows if row is not None]
