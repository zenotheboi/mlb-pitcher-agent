"""Step 2: Player ID crosswalk from the Chadwick Bureau register.

uv run python -m data_pipeline.fetch_chadwick [--force]
"""
from __future__ import annotations

import argparse

import pandas as pd

from .common import PROCESSED, RAW, get_with_retry, log, log_fetch, session

BASE = "https://raw.githubusercontent.com/chadwickbureau/register/master/data/people-{}.csv"
SUFFIXES = "0123456789abcdef"
KEEP = ["key_mlbam", "key_bbref", "key_fangraphs", "name_first", "name_last", "birth_year"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    out_dir = RAW / "chadwick"
    out_dir.mkdir(parents=True, exist_ok=True)
    s = session()
    frames = []
    for x in SUFFIXES:
        path = out_dir / f"people-{x}.csv"
        url = BASE.format(x)
        if not path.exists() or args.force:
            r = get_with_retry(s, url)
            path.write_bytes(r.content)
            log_fetch(url, r.text.count("\n") - 1, path, r.status_code)
        frames.append(pd.read_csv(path, usecols=KEEP, dtype=str, low_memory=False))
    df = pd.concat(frames, ignore_index=True)
    df = df[df["key_mlbam"].notna()].copy()
    df["key_mlbam"] = pd.to_numeric(df["key_mlbam"], errors="coerce").astype("Int32")
    df["key_fangraphs"] = pd.to_numeric(df["key_fangraphs"], errors="coerce").astype("Int32")
    df["birth_year"] = pd.to_numeric(df["birth_year"], errors="coerce").astype("Int16")
    df = df.dropna(subset=["key_mlbam"]).drop_duplicates("key_mlbam")
    PROCESSED.mkdir(parents=True, exist_ok=True)
    out = PROCESSED / "players.parquet"
    df.to_parquet(out, compression="zstd", index=False)
    log.info("wrote %s: %d players", out.name, len(df))


if __name__ == "__main__":
    main()
