"""One JSON file per research run in logs/runs/, so runs can be read and compared afterwards.

A file holds what the log can't: the findings, the report, every query with its rows, the full SQL of
failed queries, token totals, and the settings the run used. Runs are saved however they end, so a
failed run keeps the work it finished.

    ls -t logs/runs | head

Set RUNS_DIR to save elsewhere, or RUNS_DIR= (empty) to turn this off. It's off by default on Cloud
Run, where the disk is in memory and is lost with the instance.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("pitchlab")


def runs_dir() -> Path | None:
    default = "" if os.environ.get("K_SERVICE") else str(ROOT / "logs" / "runs")
    path = os.environ.get("RUNS_DIR", default)
    return Path(path) if path else None


def git_commit() -> str:
    """The code a run used, with "-dirty" if it had uncommitted changes. Empty outside a git checkout."""
    try:
        out = subprocess.run(["git", "describe", "--always", "--dirty"], cwd=ROOT, capture_output=True,
                             text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def prompt_hash(*prompts: str) -> str:
    """Tells two runs' prompts apart, even when the change wasn't committed."""
    return hashlib.sha256("\0".join(prompts).encode()).hexdigest()[:12]


def save_run(record: dict) -> Path | None:
    """Write one run's record. Never raises: a run mustn't fail because its record couldn't be saved."""
    directory = runs_dir()
    if directory is None:
        return None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = directory / f"{stamp}-{record['run_id'][:8]}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        log.exception("couldn't save the run record")
        return None
    log.info("run saved | %s", path)
    return path
