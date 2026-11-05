"""Step 4: MLB Stats API transactions, derived IL stints, and player info.

uv run python -m data_pipeline.fetch_mlb_api --seasons 2024 2025 2026 [--force]

mlb_people needs the pitcher IDs from Statcast, so run fetch_statcast first
(otherwise that part is skipped with a warning).
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import re
import time

import pandas as pd

from .common import PROCESSED, RAW, get_with_retry, log, log_fetch, session

TX_URL = "https://statsapi.mlb.com/api/v1/transactions"
PEOPLE_URL = "https://statsapi.mlb.com/api/v1/people"
SLEEP = 1.0

IL_LEN = re.compile(r"(\d+)-day injured list", re.I)
LEAVE_KEYS = ("elected free agency", "declared free agency", "released ", "retired",
              "granted free agency")
RETRO = re.compile(r"retroactive to ([A-Z][a-z]+ \d{1,2}, \d{4})")
# Moves that show a player is back on a roster. Not every return is recorded as an activation.
BACK_KEYS = (" optioned ", " recalled ", " selected the contract ")


def fetch_month(s, year: int, month: int, force: bool) -> list:
    out_dir = RAW / "mlb_api"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"transactions_{year}-{month:02d}.json"
    if not path.exists() or force:
        last = calendar.monthrange(year, month)[1]
        params = {"sportId": 1, "startDate": f"{year}-{month:02d}-01",
                  "endDate": f"{year}-{month:02d}-{last}"}
        r = get_with_retry(s, TX_URL, params=params)
        path.write_text(r.text, encoding="utf-8")
        log_fetch(r.url, len(r.json().get("transactions", [])), path, r.status_code)
        time.sleep(SLEEP)
    return json.loads(path.read_text(encoding="utf-8")).get("transactions", [])


def build_transactions(raw: list) -> pd.DataFrame:
    rows = []
    for t in raw:
        rows.append({
            "transaction_id": t.get("id"),
            "date": t.get("date"),
            "effective_date": t.get("effectiveDate"),
            "resolution_date": t.get("resolutionDate"),
            "type_code": t.get("typeCode"),
            "type_desc": t.get("typeDesc"),
            "description": t.get("description"),
            "mlbam_id": (t.get("person") or {}).get("id"),
            "name": (t.get("person") or {}).get("fullName"),
            "from_team": (t.get("fromTeam") or {}).get("name"),
            "to_team": (t.get("toTeam") or {}).get("name"),
        })
    df = pd.DataFrame(rows).drop_duplicates("transaction_id")
    for c in ["date", "effective_date", "resolution_date"]:
        df[c] = pd.to_datetime(df[c], errors="coerce").dt.date
    df["mlbam_id"] = pd.to_numeric(df["mlbam_id"], errors="coerce").astype("Int32")
    df["transaction_id"] = df["transaction_id"].astype("int64")
    df["season"] = pd.to_datetime(df["date"]).dt.year.astype("Int16")
    return df.sort_values(["date", "transaction_id"]).reset_index(drop=True)


def classify(desc: str) -> str | None:
    d = desc.lower() if isinstance(desc, str) else ""
    if "injured list" not in d:
        if any(k in d for k in LEAVE_KEYS):
            return "left_roster"
        if any(k in d for k in BACK_KEYS):
            return "back"
        return None
    if "transferred" in d and "60-day injured list" in d:
        return "transfer"
    if "placed" in d:
        return "placement"
    if "activated" in d or "reinstated" in d:
        return "activation"
    return None


def injury_note(desc: str) -> str | None:
    """Text after the sentence that mentions the injured list, e.g. 'Right elbow inflammation.'"""
    if not isinstance(desc, str):
        return None
    m = re.search(r"injured list[^.]*?(?:\d{4})?\.\s*(.+)$", desc, re.I)
    note = m.group(1).strip() if m else ""
    return note or None


def start_date(row) -> dt.date:
    m = RETRO.search(row.description if isinstance(row.description, str) else "")
    if m:
        try:
            return dt.datetime.strptime(m.group(1), "%B %d, %Y").date()
        except ValueError:
            pass
    return row.effective_date or row.date


def is_new_absence(stint: dict, placement) -> bool:
    """A placement during an open stint usually extends it (15-day to 60-day). A different injury in a
    later season doesn't: the player came back without a recorded return."""
    note = injury_note(placement.description)
    later = start_date(placement).year > stint["start_date"].year
    return bool(later and note and note not in (stint["injury_note"] or ""))


def new_stint(pid, r) -> dict:
    m = IL_LEN.search(r.description)
    return {
        "mlbam_id": pid, "name": r.name, "team": r.to_team or r.from_team,
        "il_type": f"{m.group(1)}-day" if m else None,
        "start_date": start_date(r), "end_date": None,
        "injury_note": injury_note(r.description),
        "placement_transaction_id": r.transaction_id,
        "transfer_transaction_id": None, "activation_transaction_id": None,
        "placement_description": r.description, "activation_description": None,
        "end_reason": None,
    }


def build_il_stints(tx: pd.DataFrame) -> pd.DataFrame:
    """Pair each placement with what ended it for the same player (heuristic)."""
    ev = tx.copy()
    ev["kind"] = ev["description"].map(classify)
    ev = ev[ev["kind"].notna() & ev["mlbam_id"].notna()]
    # Same-day events: close the old stint before opening a new one.
    ev["kind_order"] = ev["kind"].map({"activation": 0, "back": 0, "left_roster": 1, "transfer": 2,
                                       "placement": 3})
    ev = ev.sort_values(["mlbam_id", "date", "kind_order", "transaction_id"])
    stints = []
    for pid, g in ev.groupby("mlbam_id"):
        cur = None
        for r in g.itertuples():
            if r.kind == "placement" and cur is not None and is_new_absence(cur, r):
                cur["end_reason"] = "unknown"  # he came back, but no transaction says when
                stints.append(cur)
                cur = new_stint(pid, r)
            elif r.kind == "placement" and cur is not None:
                # Placed again while still on the IL (e.g. 15-day -> "placed on 60-day"):
                # same absence, so continue the stint and keep the new note too.
                m = IL_LEN.search(r.description)
                if m:
                    cur["il_type"] = f"{m.group(1)}-day"
                note = injury_note(r.description)
                if note and note != cur["injury_note"]:
                    cur["injury_note"] = "; ".join(x for x in [cur["injury_note"], note] if x)
                cur["transfer_transaction_id"] = r.transaction_id
            elif r.kind == "placement":
                cur = new_stint(pid, r)
            elif r.kind == "transfer" and cur is not None:
                cur["il_type"] = "60-day"
                cur["transfer_transaction_id"] = r.transaction_id
                cur["injury_note"] = cur["injury_note"] or injury_note(r.description)
            elif r.kind in ("activation", "back") and cur is not None:
                cur["end_date"] = r.effective_date or r.date
                cur["activation_transaction_id"] = r.transaction_id
                cur["activation_description"] = r.description
                cur["end_reason"] = "activated" if r.kind == "activation" else "back_on_roster"
                stints.append(cur)
                cur = None
            elif r.kind == "left_roster" and cur is not None:
                # Released / free agency while on the IL: the stint ends without activation.
                cur["end_date"] = r.effective_date or r.date
                cur["activation_transaction_id"] = r.transaction_id
                cur["activation_description"] = r.description
                cur["end_reason"] = "left_roster"
                stints.append(cur)
                cur = None
        if cur is not None:
            stints.append(cur)
    df = pd.DataFrame(stints)
    df["start_date"] = pd.to_datetime(df["start_date"]).dt.date
    df["end_date"] = pd.to_datetime(df["end_date"]).dt.date
    df["days_on_il"] = (pd.to_datetime(df["end_date"]) - pd.to_datetime(df["start_date"])).dt.days
    df["days_on_il"] = df["days_on_il"].astype("Int16")
    df["season"] = pd.to_datetime(df["start_date"]).dt.year.astype("Int16")
    for c in ["mlbam_id"]:
        df[c] = df[c].astype("Int32")
    for c in ["placement_transaction_id", "transfer_transaction_id", "activation_transaction_id"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    neg = df["days_on_il"] < 0
    if neg.any():
        log.warning("%d stints with negative length (bad pairing); setting end_date null", neg.sum())
        df.loc[neg, ["end_date", "days_on_il"]] = None
        df.loc[neg, "end_reason"] = "unknown"
    return df


def fetch_people(s, ids: list, force: bool) -> pd.DataFrame:
    out_dir = RAW / "mlb_api"
    path = out_dir / "people.json"
    cached = json.loads(path.read_text()) if path.exists() and not force else []
    have = {p["id"] for p in cached}
    todo = [i for i in ids if i not in have]
    for k in range(0, len(todo), 100):
        batch = todo[k:k + 100]
        r = get_with_retry(s, PEOPLE_URL, params={"personIds": ",".join(map(str, batch))})
        people = r.json().get("people", [])
        cached += people
        log_fetch(r.url[:200], len(people), path, r.status_code)
        time.sleep(SLEEP)
    path.write_text(json.dumps(cached), encoding="utf-8")
    rows = [{
        "mlbam_id": p.get("id"), "full_name": p.get("fullName"),
        "birth_date": p.get("birthDate"), "pitch_hand": (p.get("pitchHand") or {}).get("code"),
        "primary_position": (p.get("primaryPosition") or {}).get("abbreviation"),
        "mlb_debut_date": p.get("mlbDebutDate"),
    } for p in cached]
    df = pd.DataFrame(rows).drop_duplicates("mlbam_id")
    df["mlbam_id"] = df["mlbam_id"].astype("Int32")
    for c in ["birth_date", "mlb_debut_date"]:
        df[c] = pd.to_datetime(df[c], errors="coerce").dt.date
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", nargs="+", type=int, default=[2024, 2025, 2026])
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    s = session()
    today = dt.date.today()
    raw = []
    for y in args.seasons:
        for m in range(1, 13):
            if dt.date(y, m, 1) > today:
                break
            raw += fetch_month(s, y, m, args.force)
    tx = build_transactions(raw)
    PROCESSED.mkdir(parents=True, exist_ok=True)
    tx.to_parquet(PROCESSED / "transactions.parquet", compression="zstd", index=False)
    log.info("wrote transactions.parquet: %d rows", len(tx))

    il = build_il_stints(tx)
    il.to_parquet(PROCESSED / "il_stints.parquet", compression="zstd", index=False)
    log.info("wrote il_stints.parquet: %d stints (%d still open)", len(il), il["end_date"].isna().sum())
    sample = il.sample(min(20, len(il)), random_state=0)
    with pd.option_context("display.width", 250, "display.max_colwidth", 60):
        print("\n=== 20 random IL stints: check these by hand ===")
        print(sample[["name", "team", "il_type", "start_date", "end_date", "days_on_il",
                      "injury_note"]].to_string(index=False))

    pitch_files = sorted(PROCESSED.glob("pitches_*.parquet"))
    if not pitch_files:
        log.warning("no pitches_*.parquet yet; skipping mlb_people (re-run after fetch_statcast)")
        return
    ids = sorted(set().union(*[set(pd.read_parquet(p, columns=["pitcher"])["pitcher"].dropna()
                                   .astype(int)) for p in pitch_files]))
    people = fetch_people(s, ids, args.force)
    people.to_parquet(PROCESSED / "mlb_people.parquet", compression="zstd", index=False)
    log.info("wrote mlb_people.parquet: %d people", len(people))


if __name__ == "__main__":
    main()
