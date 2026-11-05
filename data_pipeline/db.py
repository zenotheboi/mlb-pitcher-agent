"""Runtime helpers for the agent / Cloud Run: ensure_db() and a read-only connection.

    from data_pipeline.db import ensure_db, connect_readonly
    ensure_db()            # call once at app startup
    con = connect_readonly()

Env vars:
    GCS_BUCKET        bucket name (default mlb-agent-2026)
    GCS_DATA_VERSION  data version folder (default v3)
"""
from __future__ import annotations

import os

import duckdb

from .common import DB_PATH, PROCESSED, log

BUCKET = os.environ.get("GCS_BUCKET", "mlb-agent-2026")
VERSION = os.environ.get("GCS_DATA_VERSION", "v3")
PREFIX = f"baseball-agent/{VERSION}"


def _download(names_filter) -> int:
    from google.cloud import storage  # imported lazily: only needed on cold start

    client = storage.Client()
    n = 0
    for blob in client.list_blobs(BUCKET, prefix=PREFIX + "/"):
        rel = blob.name[len(PREFIX) + 1:]
        if not names_filter(rel):
            continue
        dest = DB_PATH.parent / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(dest))
        n += 1
        log.info("downloaded gs://%s/%s (%.1f MB)", BUCKET, blob.name, blob.size / 1e6)
    return n


def ensure_db() -> str:
    """1) use data/baseball.duckdb if present; 2) else build from data/processed/*.parquet;
    3) else download from GCS (the .duckdb if uploaded, otherwise the parquet files) and build."""
    if DB_PATH.exists():
        return str(DB_PATH)
    # The repo ships only the small Parquet files; pitches_*.parquet lives in GCS.
    if not list(PROCESSED.glob("pitches_*.parquet")):
        log.info("no local pitch data; downloading from gs://%s/%s", BUCKET, PREFIX)
        if _download(lambda rel: rel == DB_PATH.name) and DB_PATH.exists():
            return str(DB_PATH)
        _download(lambda rel: rel.startswith("processed/") and rel.endswith(".parquet"))
    if not list(PROCESSED.glob("pitches_*.parquet")):
        raise RuntimeError(f"no pitch data locally or in gs://{BUCKET}/{PREFIX}/")
    from .build_db import build
    build()
    return str(DB_PATH)


def connect_readonly() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH), read_only=True)
