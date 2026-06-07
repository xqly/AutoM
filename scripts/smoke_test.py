from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autom_app.config import get_settings
from autom_app.database import connect


def main() -> None:
    settings = get_settings()
    if not settings.database_path.exists():
        raise SystemExit("Database does not exist. Run scripts/setup.py first.")
    with connect(settings) as conn:
        user_count = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
        table_count = conn.execute(
            "SELECT COUNT(*) AS count FROM sqlite_master WHERE type = 'table'"
        ).fetchone()["count"]
    print(json.dumps({"database": str(settings.database_path), "users": user_count, "tables": table_count}, indent=2))
    try:
        with urllib.request.urlopen(f"http://{settings.host}:{settings.port}/", timeout=2) as response:
            print(f"server_http_status={response.status}")
    except urllib.error.URLError:
        print("server_http_status=unreachable")


if __name__ == "__main__":
    main()
