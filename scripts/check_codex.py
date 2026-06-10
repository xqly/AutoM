from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autom_app.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Check Codex CLI readiness for AutoM.")
    parser.add_argument("--run-smoke", action="store_true", help="Run a tiny codex exec smoke test.")
    args = parser.parse_args()

    settings = get_settings()
    command_path = shutil.which(settings.codex_command)
    exists = command_path is not None or Path(settings.codex_command).exists()
    result = {
        "command": settings.codex_command,
        "found": exists,
        "path": command_path or settings.codex_command,
        "dry_run": settings.codex_dry_run,
        "model": settings.codex_model or None,
        "has_CODEX_API_KEY": bool(os.environ.get("CODEX_API_KEY")),
    }
    if not exists:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(1)

    version = subprocess.run(
        [settings.codex_command, "--version"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    result["version_returncode"] = version.returncode
    result["version_stdout"] = version.stdout.strip()
    result["version_stderr"] = version.stderr.strip()

    if args.run_smoke:
        with tempfile.TemporaryDirectory(prefix="autom-codex-smoke-") as tmp:
            tmp_path = Path(tmp)
            schema_path = tmp_path / "schema.json"
            output_path = tmp_path / "final.json"
            schema_path.write_text(
                json.dumps(
                    {
                        "type": "object",
                        "properties": {"status": {"type": "string"}, "summary": {"type": "string"}},
                        "required": ["status", "summary"],
                        "additionalProperties": False,
                    }
                ),
                encoding="utf-8",
            )
            command = [
                settings.codex_command,
                "exec",
                "--json",
                "--sandbox",
                "workspace-write",
                "--skip-git-repo-check",
                "--cd",
                str(tmp_path),
                "--output-schema",
                str(schema_path),
                "-o",
                str(output_path),
                "-",
            ]
            smoke = subprocess.run(
                command,
                input="Return status completed and summary ok. Do not create other files.",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
            result["smoke_returncode"] = smoke.returncode
            result["smoke_stdout_tail"] = smoke.stdout[-1000:]
            result["smoke_stderr_tail"] = smoke.stderr[-1000:]
            result["smoke_final_exists"] = output_path.exists()
            if output_path.exists():
                result["smoke_final"] = output_path.read_text(encoding="utf-8", errors="replace")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("version_returncode") not in (None, 0):
        raise SystemExit(1)
    if args.run_smoke and result.get("smoke_returncode") != 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
