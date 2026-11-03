"""Step 3: Baseball Reference season-level pitching tables (league pages only).

uv run python -m data_pipeline.fetch_bref --seasons 2015-2026

By default this only parses files already on disk (cached HTML in data/raw/bref/ or pages
saved by hand in data/raw/bref/manual/), because Baseball Reference blocks automated
clients. --download asks B-Ref for missing pages; if it answers 403, stop and save by hand.

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
    # Traded pitchers: the export has one row per team plus a 2TM/3TM/TOT total row.
    # Mark the per-team rows as partial so finalize() keeps only the total.
    key = "bref_id" if "bref_id" in df else "name"
    total = df["team"].astype(str).str.match(MULTI_TEAM)
    has_total = df[key].isin(df.loc[total, key])
    df["is_partial"] = has_total & ~total
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


TITLE = re.compile(r"<title>\s*(\d{4}) Major League Baseball (Standard Pitching|Value)", re.I)


def index_manual_html(manual) -> dict:
    """Map (season, page) -> saved HTML file, using the page <title>, so any file name works
    (e.g. the browser default '2017 Major League Baseball Value _ Baseball-Reference.com.html')."""
    found = {}
    for f in sorted(manual.glob("*.htm*")):
        head = f.read_text(encoding="utf-8", errors="replace")[:20000]
        m = TITLE.search(head)
        if not m:
            log.warning("manual file %s: not a B-Ref league pitching page; ignored", f.name)
            continue
        key = (int(m.group(1)), "standard" if "standard" in m.group(2).lower() else "value")
        if key in found:
            log.warning("duplicate %s %s: using %s, ignoring %s", *key, found[key].name, f.name)
            continue
        found[key] = f
    return found


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
    ap.add_argument("--force", action="store_true", help="with --download: re-download pages")
    ap.add_argument("--download", action="store_true",
                    help="request missing pages from B-Ref (it may answer 403)")
    ap.add_argument("--no-download", action="store_true",
                    help="(default) only parse cached HTML / manual files")
    args = ap.parse_args()
    args.no_download = not args.download
    seasons = parse_seasons(args.seasons)
    raw_dir = RAW / "bref"
    manual = raw_dir / "manual"
    raw_dir.mkdir(parents=True, exist_ok=True)
    s = session()
    out = {p: [] for p in PAGES}
    first = True
    manual_html = index_manual_html(manual) if manual.exists() else {}
    for season in seasons:
        for page in PAGES:
            mhtml = manual_html.get((season, page))
            if mhtml is not None:  # page saved by hand in a browser
                df = parse_html(mhtml.read_text(encoding="utf-8", errors="replace"), page, season)
                log.info("%d %s: %d rows (manual HTML)", season, page, len(df))
                out[page].append(df)
                continue
            mcsv = manual / f"{season}_{page}.csv"
            if mcsv.exists():
                out[page].append(parse_manual_csv(mcsv, season))
                continue
            path = raw_dir / f"{season}_{page}.html"
            if not path.exists() and args.no_download:
                log.warning("%d %s: no cached HTML or manual CSV; skipped", season, page)
                continue
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
