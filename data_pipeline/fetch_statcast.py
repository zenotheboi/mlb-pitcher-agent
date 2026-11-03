"""Step 1: Statcast pitch-level data from Baseball Savant.

uv run python -m data_pipeline.fetch_statcast --seasons 2024 2025 2026 [--force]

Downloads the Savant CSV endpoint directly in 3-day chunks (Option B in the plan;
pybaseball is not used because it is unmaintained). Raw CSVs are cached in
data/raw/statcast/{season}/ and never re-downloaded unless --force is given.
"""
from __future__ import annotations

import argparse
import datetime as dt
import time

import numpy as np
import pandas as pd

from .common import PROCESSED, RAW, get_with_retry, log, log_fetch, session

URL = "https://baseballsavant.mlb.com/statcast_search/csv"
ROW_CAP = 25_000
CHUNK_DAYS = 3
SLEEP = 2.5

KEEP = {
    "who_when": ["game_pk", "game_date", "game_year", "game_type", "home_team", "away_team",
                 "inning", "inning_topbot", "at_bat_number", "pitch_number", "pitcher", "batter",
                 "player_name", "p_throws", "stand"],
    "pitch": ["pitch_type", "pitch_name", "release_speed", "effective_speed", "release_spin_rate",
              "spin_axis", "pfx_x", "pfx_z", "release_pos_x", "release_pos_y", "release_pos_z",
              "release_extension", "arm_angle"],
    "location_count": ["plate_x", "plate_z", "zone", "sz_top", "sz_bot", "balls", "strikes",
                       "outs_when_up", "on_1b", "on_2b", "on_3b"],
    "outcome": ["type", "description", "events", "bb_type", "launch_speed", "launch_angle",
                "hit_distance_sc", "estimated_woba_using_speedangle",
                "estimated_ba_using_speedangle", "woba_value", "woba_denom", "babip_value",
                "iso_value", "delta_run_exp", "delta_pitcher_run_exp"],
    "swing": ["bat_speed", "swing_length"],
}
KEEP_COLS = [c for group in KEEP.values() for c in group]

SWING = {"swinging_strike", "swinging_strike_blocked", "foul", "foul_tip", "foul_bunt",
         "hit_into_play", "missed_bunt", "bunt_foul_tip"}
WHIFF = {"swinging_strike", "swinging_strike_blocked", "foul_tip", "missed_bunt"}

FLOAT32 = ["release_speed", "effective_speed", "release_spin_rate", "spin_axis", "pfx_x", "pfx_z",
           "release_pos_x", "release_pos_y", "release_pos_z", "release_extension", "arm_angle",
           "plate_x", "plate_z", "sz_top", "sz_bot", "launch_speed", "launch_angle",
           "hit_distance_sc", "estimated_woba_using_speedangle", "estimated_ba_using_speedangle",
           "woba_value", "woba_denom", "babip_value", "iso_value", "delta_run_exp",
           "delta_pitcher_run_exp", "bat_speed", "swing_length", "pfx_x_in", "pfx_z_in"]
INT_SMALL = {"inning": "Int16", "at_bat_number": "Int16", "pitch_number": "Int16", "zone": "Int16",
             "balls": "Int8", "strikes": "Int8", "outs_when_up": "Int8", "game_year": "Int16",
             "season": "Int16", "game_month": "Int8"}
INT_ID = ["game_pk", "pitcher", "batter", "on_1b", "on_2b", "on_3b"]


def season_window(season: int) -> tuple[dt.date, dt.date]:
    start = dt.date(season, 3, 1)
    end = min(dt.date(season, 10, 5), dt.date.today() - dt.timedelta(days=1))
    return start, end


def fetch_range(s, season: int, start: dt.date, end: dt.date, force: bool) -> list:
    """Fetch [start, end] inclusive; split to single days if the row cap is hit."""
    out_dir = RAW / "statcast" / str(season)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{start}_{end}.csv"
    if path.exists() and not force:
        return [path]
    params = {
        "all": "true", "type": "details", "player_type": "pitcher",
        "hfGT": "R|", "hfSea": f"{season}|",
        "game_date_gt": start.isoformat(), "game_date_lt": end.isoformat(),
    }
    r = get_with_retry(s, URL, params=params, timeout=300)
    text = r.content.decode("utf-8-sig")
    n = max(text.count("\n") - 1, 0) if text.strip() else 0
    time.sleep(SLEEP)
    if n >= ROW_CAP and start != end:
        log.warning("%s..%s returned %d rows (cap); splitting into days", start, end, n)
        paths = []
        d = start
        while d <= end:
            paths += fetch_range(s, season, d, d, force)
            d += dt.timedelta(days=1)
        return paths
    if n >= ROW_CAP:
        log.error("single day %s still hit the row cap; data may be truncated", start)
    path.write_text(text, encoding="utf-8")
    log_fetch(r.url, n, path, r.status_code)
    log.info("%s..%s: %d rows", start, end, n)
    return [path]


def download_season(season: int, force: bool) -> list:
    s = session()
    start, end = season_window(season)
    paths = []
    d = start
    while d <= end:
        chunk_end = min(d + dt.timedelta(days=CHUNK_DAYS - 1), end)
        paths += fetch_range(s, season, d, chunk_end, force)
        d = chunk_end + dt.timedelta(days=1)
    return paths


def clean(paths: list, season: int) -> pd.DataFrame:
    frames = []
    for p in paths:
        if p.stat().st_size < 10:
            continue
        frames.append(pd.read_csv(p, low_memory=False))
    df = pd.concat(frames, ignore_index=True)

    missing = [c for c in KEEP_COLS if c not in df.columns]
    if missing:
        log.warning("columns missing from download (skipped): %s", missing)
    extra = sorted(set(df.columns) - set(KEEP_COLS))
    log.info("season %d: %d raw rows, %d columns (%d not kept)", season, len(df),
             len(df.columns), len(extra))
    df = df[[c for c in KEEP_COLS if c in df.columns]]

    df = df[df["game_type"] == "R"]
    before = len(df)
    df = df.drop_duplicates()
    log.info("dropped %d exact duplicate rows", before - len(df))

    key = ["game_pk", "at_bat_number", "pitch_number"]
    dup = df.duplicated(key, keep=False)
    if dup.any():
        log.warning("%d rows share a pitch key after exact-dup removal; keeping first", dup.sum())
        df = df.drop_duplicates(key, keep="first")

    df["game_date"] = pd.to_datetime(df["game_date"]).dt.date
    df["season"] = df["game_year"]
    desc = df["description"]
    zone = pd.to_numeric(df["zone"], errors="coerce")
    df["is_swing"] = desc.isin(SWING)
    df["is_whiff"] = desc.isin(WHIFF)
    df["in_zone"] = zone.between(1, 9)
    df["is_chase"] = df["is_swing"] & zone.between(11, 14)
    df["is_called_strike"] = desc.eq("called_strike")
    df["is_csw"] = df["is_called_strike"] | df["is_whiff"]
    df["pfx_x_in"] = df["pfx_x"] * 12
    df["pfx_z_in"] = df["pfx_z"] * 12
    df["game_month"] = pd.to_datetime(df["game_date"]).dt.month

    for c in FLOAT32:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype(np.float32)
    for c, t in INT_SMALL.items():
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype(t)
    for c in INT_ID:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int32")
    for c in ["game_type", "home_team", "away_team", "inning_topbot", "p_throws", "stand",
              "pitch_type", "pitch_name", "type", "description", "events", "bb_type"]:
        if c in df:
            df[c] = df[c].astype("category")
    return df.sort_values(key).reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", nargs="+", type=int, default=[2024, 2025, 2026])
    ap.add_argument("--force", action="store_true", help="re-download cached chunks")
    args = ap.parse_args()
    PROCESSED.mkdir(parents=True, exist_ok=True)
    for season in args.seasons:
        log.info("=== Statcast %d ===", season)
        paths = download_season(season, args.force)
        df = clean(paths, season)
        out = PROCESSED / f"pitches_{season}.parquet"
        df.to_parquet(out, compression="zstd", index=False)
        log.info("wrote %s: %d rows, %.1f MB", out.name, len(df), out.stat().st_size / 1e6)


if __name__ == "__main__":
    main()
