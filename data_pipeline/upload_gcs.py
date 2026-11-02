"""Step 6: Upload data/processed/*.parquet, data/baseball.duckdb and fetch_log.csv to GCS.

uv run python -m data_pipeline.upload_gcs [--bucket mlb-agent-2026] [--version v1] [--overwrite]

Needs Google credentials (gcloud auth application-default login, or a service account).
Refuses to overwrite an existing version unless --overwrite; bump to v2 for changed data.
"""
from __future__ import annotations

import argparse

from google.cloud import storage

from .common import DB_PATH, FETCH_LOG, PROCESSED, ROOT, log


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", default="mlb-agent-2026")
    ap.add_argument("--version", default="v1")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    prefix = f"baseball-agent/{args.version}"
    client = storage.Client()
    bucket = client.bucket(args.bucket)
    existing = {b.name for b in client.list_blobs(args.bucket, prefix=prefix + "/")}
    if existing and not args.overwrite:
        raise SystemExit(f"gs://{args.bucket}/{prefix}/ already has {len(existing)} files. "
                         "Use --version v2 for new data, or --overwrite to replace.")

    files = sorted(PROCESSED.glob("*.parquet")) + [DB_PATH, FETCH_LOG, ROOT / "docs" / "schema.md"]
    total = 0
    for f in files:
        if not f.exists():
            log.warning("missing, skipped: %s", f)
            continue
        rel = f.relative_to(PROCESSED.parent) if f.is_relative_to(PROCESSED.parent) else f.relative_to(ROOT)
        blob = bucket.blob(f"{prefix}/{rel.as_posix()}")
        blob.chunk_size = 64 * 1024 * 1024
        blob.upload_from_filename(str(f), timeout=600)
        total += f.stat().st_size
        log.info("uploaded gs://%s/%s (%.1f MB)", args.bucket, blob.name, f.stat().st_size / 1e6)
    log.info("done: %.1f MB uploaded to gs://%s/%s/", total / 1e6, args.bucket, prefix)
    print(f"\nTeammates download with:\n  gsutil cp gs://{args.bucket}/{prefix}/baseball.duckdb data/")


if __name__ == "__main__":
    main()
