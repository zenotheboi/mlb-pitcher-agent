"""Shared helpers: paths, HTTP session, fetch logging."""
from __future__ import annotations

import csv
import datetime as dt
import logging
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
PROCESSED = DATA / "processed"
DB_PATH = DATA / "baseball.duckdb"
DOCS = ROOT / "docs"
FETCH_LOG = RAW / "fetch_log.csv"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pipeline")


def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def log_fetch(url: str, rows: int | None, path: Path | str, status: int | None = None) -> None:
    """Append one line to data/raw/fetch_log.csv (url, timestamp, row count)."""
    RAW.mkdir(parents=True, exist_ok=True)
    new = not FETCH_LOG.exists()
    with FETCH_LOG.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp_utc", "url", "rows", "http_status", "saved_to"])
        w.writerow([
            dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            url, rows, status, str(Path(path).relative_to(ROOT)) if Path(path).is_absolute() else path,
        ])


def get_with_retry(s: requests.Session, url: str, *, tries: int = 4, timeout: int = 180,
                   params: dict | None = None) -> requests.Response:
    """GET with exponential backoff on network errors / 5xx. Never retries 403/429 fast."""
    delay = 5
    for attempt in range(1, tries + 1):
        try:
            r = s.get(url, params=params, timeout=timeout)
            if r.status_code == 429 or r.status_code == 403:
                raise RuntimeError(
                    f"{r.status_code} from {url}: likely rate-limited/blocked. Stop and wait; "
                    "do not hammer the site.")
            if r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code} server error")
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            if attempt == tries:
                raise
            log.warning("attempt %d failed for %s (%s); retrying in %ss", attempt, url, e, delay)
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")
