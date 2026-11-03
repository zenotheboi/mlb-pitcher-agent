"""Where failures go, so they can be looked at after the run.

Everything logs to the "pitchlab" logger: stderr always (Cloud Run sends it to Cloud Logging), and
locally also logs/pitchlab.log, rotated at 2 MB. Set LOG_FILE= (empty) to turn the file off.

    tail -f logs/pitchlab.log
    grep -E "WARNING|ERROR" logs/pitchlab.log
"""

import contextvars
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORMAT = "%(asctime)s %(levelname)s [%(run)s] %(message)s"

# The run being worked on. Set once per run; asyncio tasks and tool threads inherit it, so the
# parallel researchers' lines carry it too.
current_run: contextvars.ContextVar[str] = contextvars.ContextVar("run", default="-")


class _RunId(logging.Filter):
    """Every line carries the run id (or '-'), so one run's lines can be grepped together."""

    def filter(self, record):
        record.run = current_run.get()
        return True


def setup() -> logging.Logger:
    log = logging.getLogger("pitchlab")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    log.propagate = False  # don't print twice through the root logger
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    on_cloud_run = bool(os.environ.get("K_SERVICE"))
    path = os.environ.get("LOG_FILE", "" if on_cloud_run else str(ROOT / "logs" / "pitchlab.log"))
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(logging.Formatter(FORMAT))
        h.addFilter(_RunId())
        log.addHandler(h)
    return log
