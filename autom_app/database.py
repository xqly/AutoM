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


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [row_to_dict(row) for row in rows if row is not None]
