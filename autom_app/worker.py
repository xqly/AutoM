from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings, get_settings
from .database import connect


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_name(name: str) -> str:
    allowed = []
    for char in name:
        if char.isalnum() or char in {".", "-", "_"}:
            allowed.append(char)
        else:
            allowed.append("_")
    cleaned = "".join(allowed).strip("._")
    return cleaned or "file"


class JobWorker:
    def __init__(self, settings: Settings | None = None, worker_id: str | None = None):
        self.settings = settings or get_settings()
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            claimed = self.claim_one_job()
            if claimed is None:
                self.stop_event.wait(self.settings.worker_poll_seconds)
                continue
            self.execute_job(claimed)

    def claim_one_job(self) -> dict | None:
        with connect(self.settings) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, request_id, attempt
                FROM jobs
                WHERE status = 'queued'
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            now = utc_now()
            conn.execute(
                """
                UPDATE jobs
                SET status = 'running', locked_by = ?, locked_at = ?, started_at = ?, error_message = NULL
                WHERE id = ? AND status = 'queued'
                """,
                (self.worker_id, now, now, row["id"]),
            )
            conn.execute(
                "UPDATE drawing_requests SET status = 'running', updated_at = ? WHERE id = ?",
                (now, row["request_id"]),
            )
            conn.commit()
            return {"id": row["id"], "request_id": row["request_id"], "attempt": row["attempt"]}

    def event(self, job_id: int, level: str, event_type: str, message: str, payload: dict | None = None) -> None:
        with connect(self.settings) as conn:
            conn.execute(
                """
                INSERT INTO job_events (job_id, level, event_type, message, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, level, event_type, message, json.dumps(payload or {}, ensure_ascii=False)),
            )

    def execute_job(self, job: dict) -> None:
        job_id = int(job["id"])
        request_id = int(job["request_id"])
        try:
            self.event(job_id, "info", "job.started", "Worker started the job.", {"worker_id": self.worker_id})
            job_dir = self.prepare_job_dir(job_id, request_id)
            if self.settings.codex_dry_run:
                self.run_dry_job(job_dir)
            else:
                self.run_codex(job_id, job_dir)
            self.validate_outputs(job_id, job_dir)
            self.register_artifacts(job_id, request_id, job_dir)
            final_status = self.read_final_status(job_dir)
            if final_status == "failed":
                raise RuntimeError("Codex final response reported failed.")
            request_status = "needs_clarification" if final_status == "needs_clarification" else "completed"
            self.finish_job(job_id, request_id, request_status)
        except Exception as exc:
            self.fail_job(job_id, request_id, exc)

    def prepare_job_dir(self, job_id: int, request_id: int) -> Path:
        job_dir = self.settings.data_dir / "jobs" / str(job_id)
        input_dir = job_dir / "input"
        attachments_dir = input_dir / "attachments"
        output_dir = job_dir / "output"
        logs_dir = job_dir / "logs"
        for path in (attachments_dir, output_dir, logs_dir):
            path.mkdir(parents=True, exist_ok=True)

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
            attachments = conn.execute(
                "SELECT * FROM attachments WHERE request_id = ? ORDER BY id ASC",
                (request_id,),
            ).fetchall()

        if request is None:
            raise RuntimeError(f"Request {request_id} does not exist.")

        request_payload = {key: request[key] for key in request.keys()}
        request_payload["attachments"] = []
        for item in attachments:
            source = self.settings.data_dir / item["storage_path"]
            target = attachments_dir / f"{item['id']}_{safe_name(item['original_name'])}"
            if source.exists():
                shutil.copy2(source, target)
            request_payload["attachments"].append(
                {
                    "id": item["id"],
                    "original_name": item["original_name"],
                    "path": str(target.relative_to(job_dir)),
                    "mime_type": item["mime_type"],
                    "size_bytes": item["size_bytes"],
                    "sha256": item["sha256"],
                }
            )

        (input_dir / "request.json").write_text(
            json.dumps(request_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (job_dir / "prompt.txt").write_text(self.build_prompt(request_payload), encoding="utf-8")
        (job_dir / "CAD_CONTRACT.md").write_text(cad_contract(), encoding="utf-8")
        (job_dir / "result.schema.json").write_text(
            json.dumps(result_schema(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return job_dir

    def build_prompt(self, request_payload: dict) -> str:
        attachments = request_payload.get("attachments") or []
        attachment_lines = "\n".join(
            f"- {item['path']} ({item.get('mime_type') or 'unknown'}, {item['size_bytes']} bytes)"
            for item in attachments
        ) or "- None"
        return f"""You are generating a MayCAD-ready aluminium profile design package for a customer-service drawing request.

Read CAD_CONTRACT.md first and follow it exactly. Keep all output inside the existing output/ directory.

Required files:
1. output/maycad_plan.json: structured MayCAD handoff plan for a MayTec T-slot aluminium profile assembly.
2. output/bom.csv: bill of materials with part names, profile series, quantities, lengths, and notes.
3. output/cut_list.csv: profile cut list with item IDs, profile series, lengths, quantities, and end treatment notes.
4. output/preview.png: valid PNG preview image for the website.
5. output/manifest.json: valid JSON summary with units, assumptions, dimensions, generated files, and caveats.

Important:
- Do not write outside this job directory.
- Do not ask follow-up questions. If the requirement is ambiguous, make conservative assumptions and record them in manifest.json.
- MayCAD is proprietary software. Do not invent a native .scene file unless the requested format is provided in the input. Produce a precise handoff package that a MayCAD operator can recreate and verify.
- Prefer simple, manufacturable MayTec-compatible aluminium profile structures over decorative shapes.
- The final response must conform to result.schema.json and summarize the generated files.

Request:
Title: {request_payload['title']}
Customer: {request_payload.get('customer_name') or ''}
Unit: {request_payload.get('unit') or 'mm'}
Priority: {request_payload.get('priority')}
Submitted by: {request_payload.get('created_by_name_snapshot')}

Description:
{request_payload['description']}

Reference attachments:
{attachment_lines}
"""

    def run_dry_job(self, job_dir: Path) -> None:
        output_dir = job_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "maycad_plan.json").write_text(
            json.dumps(
                {
                    "software_target": "MayCAD",
                    "unit": "mm",
                    "assumptions": ["Dry-run placeholder; Codex was not invoked."],
                    "overall_dimensions": {
                        "width_mm": 500,
                        "depth_mm": 300,
                        "height_mm": 40,
                    },
                    "profiles": [
                        {
                            "item_id": "P1",
                            "profile_series": "MayTec 40x40",
                            "length_mm": 500,
                            "quantity": 2,
                            "placement": "front/back base rails",
                        }
                    ],
                    "connectors": [
                        {
                            "item_id": "C1",
                            "description": "standard MayTec corner connector",
                            "quantity": 4,
                        }
                    ],
                    "accessories": [],
                    "machining": [],
                    "assembly_steps": [
                        "Create a 500 x 300 mm rectangular base frame in MayCAD.",
                        "Use MayTec 40x40 profiles and standard corner connectors.",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (output_dir / "bom.csv").write_text(
            "item_id,category,description,profile_series,length_mm,quantity,notes\n"
            "P1,profile,base rail,MayTec 40x40,500,2,dry-run placeholder\n"
            "C1,connector,standard corner connector,,,4,dry-run placeholder\n",
            encoding="utf-8",
        )
        (output_dir / "cut_list.csv").write_text(
            "item_id,profile_series,length_mm,quantity,end_treatment,notes\n"
            "P1,MayTec 40x40,500,2,square cut,dry-run placeholder\n",
            encoding="utf-8",
        )
        (output_dir / "preview.png").write_bytes(sample_png())
        (output_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "status": "dry_run",
                    "unit": "mm",
                    "assumptions": ["Dry-run placeholder; Codex was not invoked."],
                    "files": ["maycad_plan.json", "bom.csv", "cut_list.csv", "preview.png", "manifest.json"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def run_codex(self, job_id: int, job_dir: Path) -> None:
        command_path = shutil.which(self.settings.codex_command)
        if command_path is None and not Path(self.settings.codex_command).exists():
            raise RuntimeError(
                f"Codex command not found: {self.settings.codex_command}. "
                "Set AUTOM_CODEX_COMMAND or install Codex CLI."
            )
        prompt = (job_dir / "prompt.txt").read_text(encoding="utf-8")
        command = [
            self.settings.codex_command,
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "--cd",
            str(job_dir),
            "--output-schema",
            "result.schema.json",
            "-o",
            "final.json",
        ]
        if self.settings.codex_model:
            command.extend(["-m", self.settings.codex_model])

        for attachment in sorted((job_dir / "input" / "attachments").iterdir()):
            if attachment.is_file() and attachment.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                command.extend(["-i", str(attachment.relative_to(job_dir))])
        command.append("-")

        self.event(job_id, "info", "codex.started", "Starting codex exec.", {"command": redact_command(command)})
        env = os.environ.copy()
        with (job_dir / "logs" / "codex.jsonl").open("wb") as stdout_file, (
            job_dir / "logs" / "stderr.log"
        ).open("wb") as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=job_dir,
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                env=env,
            )
            accepted_early = self.wait_for_codex(job_id, job_dir, process, prompt)

        self.import_codex_events(job_id, job_dir / "logs" / "codex.jsonl")

        if not accepted_early and process.returncode != 0:
            stderr_tail = tail_text(job_dir / "logs" / "stderr.log")
            raise RuntimeError(f"codex exec failed with exit code {process.returncode}. {stderr_tail}")
        if accepted_early:
            self.event(job_id, "info", "codex.early_accepted", "Accepted validated output files before Codex final response.")
        else:
            self.event(job_id, "info", "codex.completed", "codex exec completed.")

    def wait_for_codex(
        self,
        job_id: int,
        job_dir: Path,
        process: subprocess.Popen,
        prompt: str,
    ) -> bool:
        if process.stdin is None:
            raise RuntimeError("codex exec stdin is not available.")
        try:
            process.stdin.write(prompt.encode("utf-8"))
            process.stdin.close()
        except BrokenPipeError as exc:
            raise RuntimeError("codex exec closed stdin before receiving the prompt.") from exc

        deadline = time.monotonic() + self.settings.codex_timeout_seconds
        stable_since: float | None = None
        stable_signature: tuple | None = None
        early_accept_seconds = max(0, self.settings.codex_early_accept_seconds)

        while True:
            return_code = process.poll()
            if return_code is not None:
                return False
            if time.monotonic() > deadline:
                process.kill()
                process.wait(timeout=10)
                raise RuntimeError(f"codex exec timed out after {self.settings.codex_timeout_seconds} seconds.")

            if early_accept_seconds > 0:
                valid, _error = self.outputs_are_valid(job_dir)
                if valid:
                    signature = output_signature(job_dir)
                    if signature != stable_signature:
                        stable_signature = signature
                        stable_since = time.monotonic()
                    elif stable_since is not None and time.monotonic() - stable_since >= early_accept_seconds:
                        self.event(
                            job_id,
                            "info",
                            "codex.early_accepting",
                            f"Output files passed validation and were stable for {early_accept_seconds} seconds.",
                        )
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=10)
                        return True
                else:
                    stable_since = None
                    stable_signature = None

            time.sleep(2)

    def validate_outputs(self, job_id: int, job_dir: Path) -> None:
        valid, error = self.outputs_are_valid(job_dir)
        if not valid:
            raise RuntimeError(error)
        self.event(
            job_id,
            "info",
            "outputs.validated",
            "Validated maycad_plan.json, bom.csv, cut_list.csv, preview.png, and manifest.json.",
        )

    def outputs_are_valid(self, job_dir: Path) -> tuple[bool, str | None]:
        output_dir = job_dir / "output"
        expected = ["maycad_plan.json", "bom.csv", "cut_list.csv", "preview.png", "manifest.json"]
        missing = [name for name in expected if not (output_dir / name).is_file()]
        if missing:
            return False, "Missing expected output file(s): " + ", ".join(missing)

        try:
            plan = json.loads((output_dir / "maycad_plan.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return False, f"output/maycad_plan.json is not valid JSON: {exc}"
        if not isinstance(plan, dict):
            return False, "output/maycad_plan.json must contain a JSON object."
        for key in (
            "software_target",
            "unit",
            "assumptions",
            "overall_dimensions",
            "profiles",
            "connectors",
            "accessories",
            "machining",
            "assembly_steps",
        ):
            if key not in plan:
                return False, f"output/maycad_plan.json missing required key: {key}"
        if "maycad" not in str(plan.get("software_target", "")).lower():
            return False, "output/maycad_plan.json must target MayCAD."

        for csv_name in ("bom.csv", "cut_list.csv"):
            csv_text = (output_dir / csv_name).read_text(encoding="utf-8", errors="replace")
            lines = [line for line in csv_text.splitlines() if line.strip()]
            if len(lines) < 2:
                return False, f"output/{csv_name} must include a header and at least one data row."
            if "," not in lines[0]:
                return False, f"output/{csv_name} must be comma-separated CSV."

        png_path = output_dir / "preview.png"
        if png_path.stat().st_size < 64 or not png_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n":
            return False, "output/preview.png is not a valid PNG file."

        try:
            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return False, f"output/manifest.json is not valid JSON: {exc}"
        if not isinstance(manifest, dict):
            return False, "output/manifest.json must contain a JSON object."
        for key in ("unit", "files"):
            if key not in manifest:
                return False, f"output/manifest.json missing required key: {key}"
        return True, None

    def import_codex_events(self, job_id: int, jsonl_path: Path) -> None:
        if not jsonl_path.exists():
            return
        imported = 0
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if imported >= 200:
                    self.event(job_id, "warning", "codex.events_truncated", "Only the first 200 Codex events were imported.")
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_type = str(payload.get("type", "codex.event"))
                message = summarize_codex_event(payload)
                if not message:
                    continue
                self.event(job_id, "info", event_type, message, payload)
                imported += 1

    def register_artifacts(self, job_id: int, request_id: int, job_dir: Path) -> None:
        expected = [
            ("maycad_plan.json", "maycad_plan", "application/json"),
            ("bom.csv", "bom_csv", "text/csv"),
            ("cut_list.csv", "cut_list_csv", "text/csv"),
            ("preview.png", "preview_png", "image/png"),
            ("manifest.json", "manifest", "application/json"),
        ]
        output_dir = job_dir / "output"
        missing = [name for name, _kind, _mime in expected if not (output_dir / name).exists()]
        if missing:
            raise RuntimeError("Missing expected output file(s): " + ", ".join(missing))

        rows = []
        for name, kind, mime_type in expected:
            rows.append(self.artifact_row(job_id, request_id, output_dir / name, kind, mime_type, name))

        logs = job_dir / "logs" / "codex.jsonl"
        if logs.exists():
            rows.append(self.artifact_row(job_id, request_id, logs, "log", "application/x-ndjson", "codex.jsonl"))
        final_json = job_dir / "final.json"
        if final_json.exists():
            rows.append(self.artifact_row(job_id, request_id, final_json, "final_json", "application/json", "final.json"))

        with connect(self.settings) as conn:
            conn.execute("DELETE FROM artifacts WHERE job_id = ?", (job_id,))
            conn.executemany(
                """
                INSERT INTO artifacts
                  (request_id, job_id, kind, storage_path, original_name, mime_type, size_bytes, sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def artifact_row(
        self,
        job_id: int,
        request_id: int,
        path: Path,
        kind: str,
        mime_type: str,
        original_name: str,
    ) -> tuple:
        relative_path = path.relative_to(self.settings.data_dir)
        return (
            request_id,
            job_id,
            kind,
            relative_path.as_posix(),
            original_name,
            mime_type,
            path.stat().st_size,
            sha256_file(path),
        )

    def read_final_status(self, job_dir: Path) -> str:
        final_json = job_dir / "final.json"
        if not final_json.exists():
            return "completed"
        try:
            payload = json.loads(final_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return "completed"
        return str(payload.get("status") or "completed")

    def finish_job(self, job_id: int, request_id: int, request_status: str) -> None:
        now = utc_now()
        with connect(self.settings) as conn:
            conn.execute(
                "UPDATE jobs SET status = 'completed', finished_at = ?, error_message = NULL WHERE id = ?",
                (now, job_id),
            )
            conn.execute(
                "UPDATE drawing_requests SET status = ?, updated_at = ? WHERE id = ?",
                (request_status, now, request_id),
            )
        self.event(job_id, "info", "job.completed", f"Job finished with request status {request_status}.")

    def fail_job(self, job_id: int, request_id: int, exc: Exception) -> None:
        now = utc_now()
        message = str(exc)
        with connect(self.settings) as conn:
            conn.execute(
                "UPDATE jobs SET status = 'failed', finished_at = ?, error_message = ? WHERE id = ?",
                (now, message, job_id),
            )
            conn.execute(
                "UPDATE drawing_requests SET status = 'failed', updated_at = ? WHERE id = ?",
                (now, request_id),
            )
        self.event(
            job_id,
            "error",
            "job.failed",
            message,
            {"traceback": traceback.format_exc(limit=20)},
        )


def result_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["completed", "failed", "needs_clarification"]},
            "summary": {"type": "string"},
            "files": {
                "type": "array",
                "items": {"type": "string"},
            },
            "assumptions": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["status", "summary", "files", "assumptions"],
        "additionalProperties": False,
    }


def cad_contract() -> str:
    return """# AutoM CAD Contract

You are producing MayCAD-ready deliverables for a customer-service aluminium profile drawing request.

MayCAD is proprietary MayTec design software for MayTec T-slot aluminium profile assemblies. Generate a clear handoff package that lets a MayCAD operator recreate, check, quote, and refine the design. Do not invent a native `.scene` file unless the request supplies a verified file-format template.

## Required Output Files

Create these exact files under `output/`:

1. `maycad_plan.json`
   - Valid JSON object.
   - Include at least:
     - `software_target`: `MayCAD`
     - `unit`
     - `assumptions`
     - `overall_dimensions`
     - `profiles`
     - `connectors`
     - `accessories`
     - `machining`
     - `assembly_steps`
   - Each profile item should include an item ID, profile series or size, length, quantity, placement/orientation, and any MayCAD entry notes.
   - Keep all dimensions in millimeters unless the request explicitly says otherwise.

2. `bom.csv`
   - Comma-separated CSV with a header row.
   - Include part/profile/connectors/accessories, descriptions, quantities, MayTec-compatible series or size, lengths if relevant, and notes.

3. `cut_list.csv`
   - Comma-separated CSV with a header row.
   - Include profile item ID, profile series or size, cut length, quantity, cut angle/end treatment, and notes.

4. `preview.png`
   - Valid PNG image.
   - Show a simple orthographic or isometric-style preview of the aluminium profile assembly.
   - It can be generated with Pillow if available, or with Python stdlib PNG writing if necessary.

5. `manifest.json`
   - Valid JSON object.
   - Include at least:
     - `unit`
     - `assumptions`
     - `dimensions`
     - `files`
     - `notes`

## Behavior

- Do not ask follow-up questions. Make conservative assumptions if the request is incomplete.
- Record all assumptions in `manifest.json`.
- Keep units in millimeters unless the request explicitly says otherwise.
- Do not write outside the job directory.
- Use simple, manufacturable MayTec-compatible aluminium profile structures and standard connector logic.
- Avoid decorative or unrelated features.
- Do one concise self-check only: JSON validity, CSV headers/data rows, PNG validity, and manifest completeness.
- Do not run open-ended correction loops. If the requested files pass basic handoff checks, produce the final response.
- Final answer must match `result.schema.json`.
"""


def output_signature(job_dir: Path) -> tuple:
    output_dir = job_dir / "output"
    names = ["maycad_plan.json", "bom.csv", "cut_list.csv", "preview.png", "manifest.json"]
    signature = []
    for name in names:
        path = output_dir / name
        if not path.exists():
            signature.append((name, None, None))
        else:
            stat = path.stat()
            signature.append((name, stat.st_size, int(stat.st_mtime)))
    return tuple(signature)


def tail_text(path: Path, limit: int = 2000) -> str:
    if not path.exists():
        return ""
    data = path.read_bytes()[-limit:]
    return data.decode("utf-8", errors="replace").strip()


def redact_command(command: list[str]) -> list[str]:
    return ["***" if item.startswith("sk-") else item for item in command]


def summarize_codex_event(payload: dict) -> str:
    event_type = payload.get("type")
    if event_type == "item.completed":
        item = payload.get("item") or {}
        if item.get("type") == "agent_message":
            return str(item.get("text") or "")[:500]
        if item.get("type") == "command_execution":
            command = item.get("command") or ""
            status = item.get("status") or ""
            exit_code = item.get("exit_code")
            return f"command={command} status={status} exit_code={exit_code}"
    if event_type == "turn.completed":
        usage = payload.get("usage") or {}
        return "usage=" + json.dumps(usage, ensure_ascii=False)
    if event_type in {"thread.started", "turn.started"}:
        return str(event_type)
    return ""


def sample_png() -> bytes:
    # A tiny valid 1x1 PNG. The frontend labels dry-run files in the task events.
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/az+4iQAAAAASUVORK5CYII="
    )


def run_worker_forever() -> None:
    JobWorker(get_settings()).run_forever()
