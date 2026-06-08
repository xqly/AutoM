from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autom_app.config import get_settings
from autom_app.database import connect, init_db
from autom_app.security import hash_password


def add_user(args: argparse.Namespace) -> None:
    password = args.password or getpass.getpass("Password: ")
    if not password:
        raise SystemExit("Password cannot be empty.")
    settings = get_settings()
    init_db(settings)
    with connect(settings) as conn:
        conn.execute(
            """
            INSERT INTO users (username, display_name, password_hash, role, is_active)
            VALUES (?, ?, ?, ?, 1)
            """,
            (args.username, args.display_name, hash_password(password), args.role),
        )
    print(f"Created user: {args.username}")


def set_password(args: argparse.Namespace) -> None:
    password = args.password or getpass.getpass("New password: ")
    if not password:
        raise SystemExit("Password cannot be empty.")
    settings = get_settings()
    with connect(settings) as conn:
        cursor = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (hash_password(password), args.username),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = ?)", (args.username,))
    if cursor.rowcount == 0:
        raise SystemExit(f"User not found: {args.username}")
    print(f"Updated password and cleared sessions for: {args.username}")


def list_users(_args: argparse.Namespace) -> None:
    settings = get_settings()
    with connect(settings) as conn:
        rows = conn.execute(
            "SELECT id, username, display_name, role, is_active, created_at FROM users ORDER BY id ASC"
        ).fetchall()
    for row in rows:
        status = "active" if row["is_active"] else "inactive"
        print(f"{row['id']}\t{row['username']}\t{row['display_name']}\t{row['role']}\t{status}\t{row['created_at']}")


def set_active(args: argparse.Namespace, active: int) -> None:
    settings = get_settings()
    with connect(settings) as conn:
        cursor = conn.execute("UPDATE users SET is_active = ? WHERE username = ?", (active, args.username))
        if not active:
            conn.execute("DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = ?)", (args.username,))
    if cursor.rowcount == 0:
        raise SystemExit(f"User not found: {args.username}")
    print(("Activated" if active else "Deactivated") + f" user: {args.username}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage AutoM users.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("add", help="Create a user")
    add.add_argument("username")
    add.add_argument("display_name")
    add.add_argument("--role", choices=["support", "admin"], default="support")
    add.add_argument("--password", help="Password. Omit to type securely.")
    add.set_defaults(func=add_user)

    password = subparsers.add_parser("set-password", help="Set a user's password")
    password.add_argument("username")
    password.add_argument("--password", help="Password. Omit to type securely.")
    password.set_defaults(func=set_password)

    list_cmd = subparsers.add_parser("list", help="List users")
    list_cmd.set_defaults(func=list_users)

    deactivate = subparsers.add_parser("deactivate", help="Disable a user")
    deactivate.add_argument("username")
    deactivate.set_defaults(func=lambda args: set_active(args, 0))

    activate = subparsers.add_parser("activate", help="Enable a user")
    activate.add_argument("username")
    activate.set_defaults(func=lambda args: set_active(args, 1))
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
