from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autom_app.config import get_settings
from autom_app.database import connect, init_db
from autom_app.security import hash_password


def ensure_user(username: str, display_name: str, password: str, role: str) -> None:
    settings = get_settings()
    with connect(settings) as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing is not None:
            return
        conn.execute(
            """
            INSERT INTO users (username, display_name, password_hash, role)
            VALUES (?, ?, ?, ?)
            """,
            (username, display_name, hash_password(password), role),
        )


def main() -> None:
    settings = get_settings()
    init_db(settings)
    ensure_user("admin", "管理员", "admin123", "admin")
    ensure_user("support", "客服", "support123", "support")
    print(f"Initialized database: {settings.database_path}")
    print("Default users:")
    print("  admin / admin123")
    print("  support / support123")
    print("Change these passwords before production use.")


if __name__ == "__main__":
    main()
