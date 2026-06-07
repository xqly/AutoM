from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    data_dir: Path
    database_path: Path
    worker_enabled: bool
    codex_command: str
    codex_model: str
    codex_dry_run: bool
    codex_timeout_seconds: int
    worker_poll_seconds: int
    max_attachment_bytes: int
    session_ttl_hours: int


def get_settings() -> Settings:
    load_dotenv()
    data_dir = Path(os.environ.get("AUTOM_DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    return Settings(
        host=os.environ.get("AUTOM_HOST", "127.0.0.1"),
        port=env_int("AUTOM_PORT", 8000),
        data_dir=data_dir,
        database_path=data_dir / "autom.sqlite3",
        worker_enabled=env_bool("AUTOM_WORKER_ENABLED", True),
        codex_command=os.environ.get("AUTOM_CODEX_COMMAND", "codex"),
        codex_model=os.environ.get("AUTOM_CODEX_MODEL", ""),
        codex_dry_run=env_bool("AUTOM_CODEX_DRY_RUN", False),
        codex_timeout_seconds=env_int("AUTOM_CODEX_TIMEOUT_SECONDS", 1800),
        worker_poll_seconds=env_int("AUTOM_WORKER_POLL_SECONDS", 3),
        max_attachment_bytes=env_int("AUTOM_MAX_ATTACHMENT_BYTES", 10 * 1024 * 1024),
        session_ttl_hours=env_int("AUTOM_SESSION_TTL_HOURS", 72),
    )
