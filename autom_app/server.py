from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import shutil
import threading
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import PROJECT_ROOT, Settings, get_settings
from .database import connect, init_db, row_to_dict, rows_to_dicts
from .security import new_session_id, verify_password
from .worker import JobWorker, safe_name, sha256_file, utc_now


FRONTEND_DIR = PROJECT_ROOT / "frontend"


class AutomHandler(BaseHTTPRequestHandler):
    server_version = "AutoM/0.1"

    @property
    def settings(self) -> Settings:
        return self.server.settings  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self.serve_file(FRONTEND_DIR / "index.html")
        if parsed.path.startswith("/static/"):
            return self.serve_file(FRONTEND_DIR / parsed.path.removeprefix("/static/"))
        if parsed.path == "/api/me":
            return self.handle_me()
        if parsed.path == "/api/health":
            return self.handle_health()
        if parsed.path == "/api/tasks":
            return self.handle_list_tasks(parsed.query)
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/events"):
            return self.handle_task_events(parsed.path)
        if parsed.path.startswith("/api/tasks/"):
            return self.handle_get_task(parsed.path)
        if parsed.path.startswith("/api/artifacts/") and parsed.path.endswith("/download"):
            return self.handle_download_artifact(parsed.path)
        if parsed.path.startswith("/api/attachments/") and parsed.path.endswith("/download"):
            return self.handle_download_attachment(parsed.path)
        self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/auth/login":
            return self.handle_login()
        if parsed.path == "/api/auth/logout":
            return self.handle_logout()
        if parsed.path == "/api/tasks":
            return self.handle_create_task()
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/retry"):
            return self.handle_retry_task(parsed.path)
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/cancel"):
            return self.handle_cancel_task(parsed.path)
        self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def read_json(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length)
        return json.loads(raw.decode("utf-8"))

    def write_json(self, status: HTTPStatus, payload: dict | list) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.write_json(status, {"error": message})

    def serve_file(self, path: Path) -> None:
        try:
            resolved = path.resolve()
            if not resolved.is_file() or not str(resolved).startswith(str(FRONTEND_DIR.resolve())):
                return self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            data = resolved.read_bytes()
            mime_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to read file.")

    def current_user(self) -> dict | None:
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return None
        cookie = SimpleCookie(cookie_header)
        morsel = cookie.get("session_id")
        if morsel is None:
            return None
        session_id = morsel.value
        with connect(self.settings) as conn:
            row = conn.execute(
                """
                SELECT u.id, u.username, u.display_name, u.role
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.id = ? AND datetime(s.expires_at) > datetime('now') AND u.is_active = 1
                """,
                (session_id,),
            ).fetchone()
        return row_to_dict(row)

    def require_user(self) -> dict | None:
        user = self.current_user()
        if user is None:
            self.send_error_json(HTTPStatus.UNAUTHORIZED, "Login required.")
            return None
        return user

    def handle_login(self) -> None:
        payload = self.read_json()
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        with connect(self.settings) as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ? AND is_active = 1",
                (username,),
            ).fetchone()
            if row is None or not verify_password(password, row["password_hash"]):
                return self.send_error_json(HTTPStatus.UNAUTHORIZED, "Invalid username or password.")
            session_id = new_session_id()
            expires_at = datetime.now(timezone.utc) + timedelta(hours=self.settings.session_ttl_hours)
            conn.execute(
                "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
                (session_id, row["id"], expires_at.strftime("%Y-%m-%d %H:%M:%S")),
            )
        body = json.dumps(
            {
                "user": {
                    "id": row["id"],
                    "username": row["username"],
                    "display_name": row["display_name"],
                    "role": row["role"],
                }
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", f"session_id={session_id}; HttpOnly; Path=/; SameSite=Lax")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_logout(self) -> None:
        cookie_header = self.headers.get("Cookie")
        if cookie_header:
            cookie = SimpleCookie(cookie_header)
            morsel = cookie.get("session_id")
            if morsel is not None:
                with connect(self.settings) as conn:
                    conn.execute("DELETE FROM sessions WHERE id = ?", (morsel.value,))
        self.send_response(HTTPStatus.NO_CONTENT.value)
        self.send_header("Set-Cookie", "session_id=; HttpOnly; Path=/; SameSite=Lax; Max-Age=0")
        self.end_headers()

    def handle_me(self) -> None:
        user = self.current_user()
        if user is None:
            return self.write_json(HTTPStatus.OK, {"user": None})
        self.write_json(HTTPStatus.OK, {"user": user})

    def handle_health(self) -> None:
        user = self.require_user()
        if user is None:
            return
        with connect(self.settings) as conn:
            task_rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM drawing_requests GROUP BY status"
            ).fetchall()
            job_rows = conn.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
        codex_path = shutil.which(self.settings.codex_command)
        payload = {
            "ok": True,
            "dry_run": self.settings.codex_dry_run,
            "worker_enabled": self.settings.worker_enabled,
            "data_dir": str(self.settings.data_dir),
            "database_path": str(self.settings.database_path),
            "codex": {
                "command": self.settings.codex_command,
                "found": codex_path is not None or Path(self.settings.codex_command).exists(),
                "path": codex_path or self.settings.codex_command,
                "model": self.settings.codex_model or None,
            },
            "tasks": {row["status"]: row["count"] for row in task_rows},
            "jobs": {row["status"]: row["count"] for row in job_rows},
        }
        self.write_json(HTTPStatus.OK, payload)

    def handle_create_task(self) -> None:
        user = self.require_user()
        if user is None:
            return
        payload = self.read_json()
        description = str(payload.get("description", "")).strip()
        if not description:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "Description is required.")
        customer_name = str(payload.get("customer_name", "")).strip()
        title = derive_title(customer_name, description)
        unit = "mm"
        priority = 3
        attachments = payload.get("attachments") or []
        if not isinstance(attachments, list):
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "attachments must be a list.")

        with connect(self.settings) as conn:
            cursor = conn.execute(
                """
                INSERT INTO drawing_requests
                  (title, customer_name, description, unit, priority, status, created_by, created_by_name_snapshot)
                VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (title, customer_name, description, unit, priority, user["id"], user["display_name"]),
            )
            request_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO jobs (request_id, status, attempt, runner) VALUES (?, 'queued', 1, 'codex_exec')",
                (request_id,),
            )

        try:
            self.save_attachments(int(request_id), attachments)
        except ValueError as exc:
            with connect(self.settings) as conn:
                conn.execute("DELETE FROM drawing_requests WHERE id = ?", (request_id,))
            return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

        self.write_json(HTTPStatus.CREATED, {"id": request_id})

    def save_attachments(self, request_id: int, attachments: list[dict]) -> None:
        target_dir = self.settings.data_dir / "attachments" / str(request_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for index, item in enumerate(attachments, start=1):
            name = safe_name(str(item.get("name", f"attachment_{index}")))
            mime_type = str(item.get("mime_type", "application/octet-stream"))
            data_base64 = str(item.get("data_base64", ""))
            try:
                data = base64.b64decode(data_base64, validate=True)
            except Exception as exc:
                raise ValueError(f"Attachment {name} is not valid base64.") from exc
            if len(data) > self.settings.max_attachment_bytes:
                raise ValueError(f"Attachment {name} exceeds max size.")
            path = target_dir / f"{index}_{name}"
            path.write_bytes(data)
            relative_path = path.relative_to(self.settings.data_dir).as_posix()
            rows.append(
                (
                    request_id,
                    name,
                    relative_path,
                    mime_type,
                    len(data),
                    hashlib.sha256(data).hexdigest(),
                )
            )
        if rows:
            with connect(self.settings) as conn:
                conn.executemany(
                    """
                    INSERT INTO attachments
                      (request_id, original_name, storage_path, mime_type, size_bytes, sha256)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

    def handle_list_tasks(self, query: str) -> None:
        user = self.require_user()
        if user is None:
            return
        params = parse_qs(query)
        status = (params.get("status") or [""])[0]
        sql = """
            SELECT r.*, u.username
            FROM drawing_requests r
            JOIN users u ON u.id = r.created_by
        """
        args: list[str] = []
        if status:
            sql += " WHERE r.status = ?"
            args.append(status)
        sql += " ORDER BY r.created_at DESC, r.id DESC LIMIT 200"
        with connect(self.settings) as conn:
            rows = conn.execute(sql, args).fetchall()
        self.write_json(HTTPStatus.OK, {"tasks": rows_to_dicts(rows)})

    def handle_get_task(self, path: str) -> None:
        user = self.require_user()
        if user is None:
            return
        request_id = parse_id(path, "/api/tasks/")
        if request_id is None:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
        with connect(self.settings) as conn:
            request = conn.execute(
                """
                SELECT r.*, u.username
                FROM drawing_requests r
                JOIN users u ON u.id = r.created_by
                WHERE r.id = ?
                """,
                (request_id,),
            ).fetchone()
            if request is None:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "Task not found.")
            jobs = conn.execute("SELECT * FROM jobs WHERE request_id = ? ORDER BY id DESC", (request_id,)).fetchall()
            attachments = conn.execute(
                "SELECT id, original_name, mime_type, size_bytes, sha256, created_at FROM attachments WHERE request_id = ?",
                (request_id,),
            ).fetchall()
            artifacts = conn.execute(
                "SELECT id, kind, original_name, mime_type, size_bytes, sha256, created_at FROM artifacts WHERE request_id = ? ORDER BY id ASC",
                (request_id,),
            ).fetchall()
        self.write_json(
            HTTPStatus.OK,
            {
                "task": row_to_dict(request),
                "jobs": rows_to_dicts(jobs),
                "attachments": rows_to_dicts(attachments),
                "artifacts": rows_to_dicts(artifacts),
            },
        )

    def handle_task_events(self, path: str) -> None:
        user = self.require_user()
        if user is None:
            return
        request_id = parse_id(path.removesuffix("/events"), "/api/tasks/")
        if request_id is None:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
        with connect(self.settings) as conn:
            events = conn.execute(
                """
                SELECT e.*
                FROM job_events e
                JOIN jobs j ON j.id = e.job_id
                WHERE j.request_id = ?
                ORDER BY e.created_at ASC, e.id ASC
                LIMIT 500
                """,
                (request_id,),
            ).fetchall()
        self.write_json(HTTPStatus.OK, {"events": rows_to_dicts(events)})

    def handle_retry_task(self, path: str) -> None:
        user = self.require_user()
        if user is None:
            return
        request_id = parse_id(path.removesuffix("/retry"), "/api/tasks/")
        if request_id is None:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
        with connect(self.settings) as conn:
            request = conn.execute("SELECT * FROM drawing_requests WHERE id = ?", (request_id,)).fetchone()
            if request is None:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "Task not found.")
            latest = conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) AS attempt FROM jobs WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            attempt = int(latest["attempt"]) + 1
            conn.execute(
                "INSERT INTO jobs (request_id, status, attempt, runner) VALUES (?, 'queued', ?, 'codex_exec')",
                (request_id, attempt),
            )
            conn.execute(
                "UPDATE drawing_requests SET status = 'queued', updated_at = ? WHERE id = ?",
                (utc_now(), request_id),
            )
        self.write_json(HTTPStatus.CREATED, {"ok": True})

    def handle_cancel_task(self, path: str) -> None:
        user = self.require_user()
        if user is None:
            return
        request_id = parse_id(path.removesuffix("/cancel"), "/api/tasks/")
        if request_id is None:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
        with connect(self.settings) as conn:
            conn.execute(
                "UPDATE jobs SET status = 'cancelled', finished_at = ? WHERE request_id = ? AND status = 'queued'",
                (utc_now(), request_id),
            )
            conn.execute(
                "UPDATE drawing_requests SET status = 'cancelled', updated_at = ? WHERE id = ? AND status = 'queued'",
                (utc_now(), request_id),
            )
        self.write_json(HTTPStatus.OK, {"ok": True})

    def handle_download_artifact(self, path: str) -> None:
        user = self.require_user()
        if user is None:
            return
        artifact_id = parse_id(path.removesuffix("/download"), "/api/artifacts/")
        if artifact_id is None:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
        with connect(self.settings) as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Artifact not found.")
        path = (self.settings.data_dir / row["storage_path"]).resolve()
        data_root = self.settings.data_dir.resolve()
        if not path.is_file() or not str(path).startswith(str(data_root)):
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Artifact file not found.")
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", row["mime_type"] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        disposition = "inline" if row["kind"] == "preview_png" else "attachment"
        self.send_header("Content-Disposition", f'{disposition}; filename="{row["original_name"]}"')
        self.end_headers()
        self.wfile.write(data)

    def handle_download_attachment(self, path: str) -> None:
        user = self.require_user()
        if user is None:
            return
        attachment_id = parse_id(path.removesuffix("/download"), "/api/attachments/")
        if attachment_id is None:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
        with connect(self.settings) as conn:
            row = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
        if row is None:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Attachment not found.")
        file_path = (self.settings.data_dir / row["storage_path"]).resolve()
        data_root = self.settings.data_dir.resolve()
        if not file_path.is_file() or not str(file_path).startswith(str(data_root)):
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Attachment file not found.")
        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", row["mime_type"] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        disposition = "inline" if str(row["mime_type"] or "").startswith("image/") else "attachment"
        self.send_header("Content-Disposition", f'{disposition}; filename="{row["original_name"]}"')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        print("%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), format % args))


def parse_id(path: str, prefix: str) -> int | None:
    if not path.startswith(prefix):
        return None
    text = path.removeprefix(prefix).strip("/")
    if "/" in text or not text.isdigit():
        return None
    return int(text)


def derive_title(customer_name: str, description: str) -> str:
    normalized = " ".join(description.split())
    if len(normalized) > 28:
        normalized = normalized[:28] + "..."
    if customer_name and normalized:
        return f"{customer_name} - {normalized}"
    if normalized:
        return normalized
    return "未命名绘图需求"


class AutomHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass, settings: Settings):
        super().__init__(server_address, RequestHandlerClass)
        self.settings = settings


def run_server(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    init_db(settings)
    worker: JobWorker | None = None
    worker_thread: threading.Thread | None = None
    if settings.worker_enabled:
        worker = JobWorker(settings)
        worker_thread = threading.Thread(target=worker.run_forever, name="autom-worker", daemon=True)
        worker_thread.start()
        print("Worker started in-process.")
    server = AutomHTTPServer((settings.host, settings.port), AutomHandler, settings)
    print(f"AutoM server listening on http://{settings.host}:{settings.port}")
    try:
        server.serve_forever()
    finally:
        if worker is not None:
            worker.stop()
        if worker_thread is not None:
            worker_thread.join(timeout=5)
