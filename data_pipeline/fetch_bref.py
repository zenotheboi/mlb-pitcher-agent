"""Step 3: Baseball Reference season-level pitching tables (league pages only).

uv run python -m data_pipeline.fetch_bref --seasons 2015-2026 [--force]

Rate limit: one request every >= 6 seconds (B-Ref blocks > 20 req/min, sometimes for a day).
Raw HTML is cached in data/raw/bref/{season}_{page}.html and parsed from disk.
Manual fallback: put "Get table as CSV" exports in data/raw/bref/manual/{season}_{page}.csv.
"""
from __future__ import annotations

import argparse
import re
import time

import numpy as np
import pandas as pd
from bs4 import BeautifulSoup, Comment

from .common import PROCESSED, RAW, get_with_retry, log, log_fetch, session

URL = "https://www.baseball-reference.com/leagues/majors/{season}-{page}-pitching.shtml"
PAGES = ["standard", "value"]
SLEEP = 7

# data-stat -> our column name
STANDARD_MAP = {
    "name_display": "name", "age": "age", "team_name_abbr": "team", "comp_name_abbr": "lg",
    "p_w": "W", "p_l": "L", "p_earned_run_avg": "ERA", "p_g": "G", "p_gs": "GS", "p_sv": "SV",
    "p_ip": "ip_raw", "p_h": "H", "p_r": "R", "p_er": "ER", "p_hr": "HR", "p_bb": "BB",
    "p_ibb": "IBB", "p_so": "SO", "p_hbp": "HBP", "p_bfp": "BF",
    "p_earned_run_avg_plus": "ERA_plus", "p_fip": "FIP", "p_whip": "WHIP",
    "p_hits_per_nine": "H9", "p_hr_per_nine": "HR9", "p_bb_per_nine": "BB9",
    "p_so_per_nine": "SO9", "p_strikeouts_per_base_on_balls": "SO_W",
}
VALUE_MAP = {
    "name_display": "name", "age": "age", "team_name_abbr": "team", "comp_name_abbr": "lg",
    "p_ip": "ip_raw", "p_g": "G", "p_gs": "GS", "p_ra9": "RA9", "p_raa": "RAA", "p_waa": "WAA",
    "p_rar": "RAR", "p_war": "WAR",
}
# column headers in a manual "Get table as CSV" export -> our names
CSV_MAP = {
    "Player": "name", "Age": "age", "Team": "team", "Tm": "team", "Lg": "lg", "W": "W", "L": "L",
    "ERA": "ERA", "G": "G", "GS": "GS", "SV": "SV", "IP": "ip_raw", "H": "H", "R": "R", "ER": "ER",
    "HR": "HR", "BB": "BB", "IBB": "IBB", "SO": "SO", "HBP": "HBP", "BF": "BF", "ERA+": "ERA_plus",
    "FIP": "FIP", "WHIP": "WHIP", "H9": "H9", "HR9": "HR9", "BB9": "BB9", "SO9": "SO9",
    "SO/BB": "SO_W", "SO/W": "SO_W", "RA9": "RA9", "RAA": "RAA", "WAA": "WAA", "RAR": "RAR",
    "WAR": "WAR", "Player-additional": "bref_id",
}
MULTI_TEAM = re.compile(r"^(TOT|\d+TM)$")


def ip_to_decimal(x) -> float:
    """Baseball notation -> real innings. '6.1' = 6 1/3, '6.2' = 6 2/3."""
    if x is None or (isinstance(x, float) and np.isnan(x)) or str(x).strip() == "":
        return np.nan
    whole, _, frac = str(x).partition(".")
    return int(whole) + {"": 0, "0": 0, "1": 1 / 3, "2": 2 / 3}[frac or ""]


def find_table(html: str, page: str):
    """Find the players table, searching inside HTML comments too."""
    want = f"players_{page}_pitching"
    soup = BeautifulSoup(html, "lxml")
    found = {t.get("id"): t for t in soup.find_all("table")}
    for c in soup.find_all(string=lambda x: isinstance(x, Comment)):
        if "<table" in c:
            for t in BeautifulSoup(c, "lxml").find_all("table"):
                found.setdefault(t.get("id"), t)
    log.debug("table ids: %s", list(found))
    if want in found:
        return found[want], list(found)
    # fall back: any id containing page + pitching that is not postseason
    for tid, t in found.items():
        if tid and page in tid and "pitching" in tid and "post" not in tid and "team" not in tid:
            return t, list(found)
    raise ValueError(f"no {want} table; ids found: {list(found)}")


def parse_html(html: str, page: str, season: int) -> pd.DataFrame:
    table, _ = find_table(html, page)
    cmap = STANDARD_MAP if page == "standard" else VALUE_MAP
    rows = []
    for tr in table.find("tbody").find_all("tr"):
        cls = tr.get("class") or []
        if "thead" in cls or "over_header" in cls:
            continue
        cells = {c.get("data-stat"): c for c in tr.find_all(["th", "td"])}
        name_cell = cells.get("name_display")
        if name_cell is None:
            continue
        name = name_cell.get_text(strip=True)
        if not name or "League Average" in name or name == "Player":
            continue
        bref_id = name_cell.get("data-append-csv")
        if not bref_id:
            a = name_cell.find("a")
            m = re.search(r"/players/\w/(\w+)\.shtml", a["href"]) if a else None
            bref_id = m.group(1) if m else None
        rec = {"season": season, "bref_id": bref_id, "is_partial": "partial_table" in cls}
        for stat, col in cmap.items():
            if stat in cells:
                rec[col] = cells[stat].get_text(strip=True)
        rows.append(rec)
    return pd.DataFrame(rows)


def parse_manual_csv(path, season: int) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df.rename(columns={c: CSV_MAP[c] for c in df.columns if c in CSV_MAP})
    df = df[df["name"].notna() & ~df["name"].astype(str).str.contains("League Average|^Player$")]
    df["season"] = season
    df["is_partial"] = False  # B-Ref CSV export keeps only one row per player-team; see README
    return df


def finalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["name"] = df["name"].astype(str).str.replace(r"[*#+]+$", "", regex=True).str.strip()
    # Traded players: keep the multi-team total row, set team="TOT", keep team list separately.
    teams = (df[df["is_partial"]].groupby(["season", "bref_id"])["team"]
             .agg(lambda s: ",".join(s)).rename("teams"))
    has_total = df["team"].astype(str).str.match(MULTI_TEAM)
    df.loc[has_total, "team_raw"] = df.loc[has_total, "team"]
    df.loc[has_total, "team"] = "TOT"
    df = df[~df["is_partial"]].drop(columns="is_partial")
    df = df.merge(teams, on=["season", "bref_id"], how="left")
    df["teams"] = df["teams"].fillna(df["team"])
    df["ip_decimal"] = df["ip_raw"].map(ip_to_decimal)
    text_cols = {"season", "bref_id", "name", "team", "team_raw", "teams", "lg", "ip_raw"}
    for c in df.columns:
        if c not in text_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["season"] = df["season"].astype("int16")
    return df


def attach_mlbam(df: pd.DataFrame, page: str) -> pd.DataFrame:
    players_path = PROCESSED / "players.parquet"
    if not players_path.exists():
        log.warning("players.parquet missing; run fetch_chadwick first. mlbam_id will be null.")
        df["mlbam_id"] = pd.array([pd.NA] * len(df), dtype="Int32")
        return df
    players = pd.read_parquet(players_path, columns=["key_bbref", "key_mlbam"]).dropna()
    players = players.drop_duplicates("key_bbref")
    df = df.merge(players.rename(columns={"key_bbref": "bref_id", "key_mlbam": "mlbam_id"}),
                  on="bref_id", how="left")
    miss = df[df["mlbam_id"].isna()]
    log.info("%s: %d/%d rows matched an MLBAM id", page, len(df) - len(miss), len(df))
    if len(miss):
        p = RAW / "bref" / f"unmatched_{page}.csv"
        miss[["season", "bref_id", "name", "team"]].to_csv(p, index=False)
        log.info("unmatched rows written to %s", p)
    return df


def parse_seasons(spec: list) -> list:
    out = []
    for s in spec:
        if "-" in s:
            a, b = s.split("-")
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(s))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", nargs="+", default=["2015-2026"])
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    seasons = parse_seasons(args.seasons)
    raw_dir = RAW / "bref"
    manual = raw_dir / "manual"
    raw_dir.mkdir(parents=True, exist_ok=True)
    s = session()
    out = {p: [] for p in PAGES}
    first = True
    for season in seasons:
        for page in PAGES:
            mcsv = manual / f"{season}_{page}.csv"
            if mcsv.exists():
                out[page].append(parse_manual_csv(mcsv, season))
                continue
            path = raw_dir / f"{season}_{page}.html"
            if not path.exists() or args.force:
                url = URL.format(season=season, page=page)
                r = get_with_retry(s, url, tries=2)
                path.write_text(r.text, encoding="utf-8")
                df = parse_html(r.text, page, season)
                log_fetch(url, len(df), path, r.status_code)
                time.sleep(SLEEP)
            html = path.read_text(encoding="utf-8")
            if first:
                log.info("table ids on first page: %s", find_table(html, page)[1])
                first = False
            df = parse_html(html, page, season)
            log.info("%d %s: %d rows", season, page, len(df))
            out[page].append(df)
    for page in PAGES:
        df = attach_mlbam(finalize(pd.concat(out[page], ignore_index=True)), page)
        if page == "value":
            df = df[[c for c in ["season", "bref_id", "mlbam_id", "name", "age", "team", "teams",
                                 "team_raw", "lg", "G", "GS", "ip_raw", "ip_decimal", "RA9", "RAA",
                                 "WAA", "RAR", "WAR"] if c in df]]
        p = PROCESSED / f"bref_pitching_{page}.parquet"
        df.to_parquet(p, compression="zstd", index=False)
        log.info("wrote %s: %d rows", p.name, len(df))


if __name__ == "__main__":
    main()
