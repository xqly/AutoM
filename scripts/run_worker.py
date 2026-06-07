from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autom_app.worker import run_worker_forever


if __name__ == "__main__":
    run_worker_forever()
