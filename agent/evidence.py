"""The evidence layer: read-only SQL over data/baseball.duckdb, where every query becomes a
citable source.

Every query an agent runs is stored as an Evidence record with an id like Q7. Tools return the
rows to the model under that id, the Researchers cite [Q7] in their findings, the Writer keeps
those citations, and the page shows the exact SQL, rows and upstream source behind each one.
Because the record holds exactly the rows the model saw, verify.py can check the report's
numbers against them.
"""

from __future__ import annotations

import datetime as dt
import difflib
import math
import re
import threading
from dataclasses import asdict, dataclass, field
from decimal import Decimal

import duckdb

from data_pipeline.common import DB_PATH

# Rows a model sees per query. Enough for a season-by-month table; more would crowd its context.
MAX_ROWS = 60
# A runaway query (a cross join over 2M pitches) is interrupted after this many seconds.
QUERY_TIMEOUT_S = 20

TABLES = [
    "pitches", "pitcher_season", "pitcher_season_pitch_type", "pitcher_month", "pitcher_game",
    "bref_pitching_standard", "bref_pitching_value", "il_stints", "transactions", "mlb_people",
    "players", "data_dictionary",
]

# Which upstream source each table came from. Shown on every citation.
SOURCE_OF = {
    "pitches": "statcast", "pitcher_season": "statcast", "pitcher_season_pitch_type": "statcast",
    "pitcher_month": "statcast", "pitcher_game": "statcast",
    "bref_pitching_standard": "bref", "bref_pitching_value": "bref",
    "il_stints": "mlb_api", "transactions": "mlb_api", "mlb_people": "mlb_api",
    "players": "chadwick", "data_dictionary": "internal",
}
SOURCE_LABEL = {
    "statcast": "Baseball Savant (Statcast)",
    "bref": "Baseball Reference",
    "mlb_api": "MLB Stats API",
    "chadwick": "Chadwick Bureau register",
    "internal": "Data dictionary",
}

_con: duckdb.DuckDBPyConnection | None = None
_lock = threading.Lock()


def connection() -> duckdb.DuckDBPyConnection:
    """One read-only connection per process. External access is off, so SQL from a model can't
    read local files or URLs (read_csv('/etc/passwd'), httpfs) either."""
    global _con
    with _lock:
        if _con is None:
            _con = duckdb.connect(str(DB_PATH), read_only=True, config={"enable_external_access": False})
        return _con


@dataclass
class Evidence:
    id: str
    by: str  # the agent that ran it
    purpose: str
    sql: str
    columns: list[str]
    rows: list[list]
    truncated: bool
    tables: list[str]
    sources: list[dict]  # [{"key", "label", "url"}]
    elapsed_ms: int

    def public(self) -> dict:
        return asdict(self)


@dataclass
class Ledger:
    """Every query of one research run, in order. Shared by all its agents."""

    items: dict[str, Evidence] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def next_id(self) -> str:
        with self._lock:
            qid = f"Q{len(self.items) + 1}"
            self.items[qid] = None  # reserve the id; parallel researchers get distinct ones
            return qid

    def put(self, ev: Evidence) -> None:
        with self._lock:
            self.items[ev.id] = ev

    def get(self, qid: str) -> Evidence | None:
        return self.items.get(qid)

    def public(self) -> dict[str, dict]:
        return {k: v.public() for k, v in self.items.items() if v is not None}


# --- Running SQL safely ---

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|export|import|install|load|"
    r"pragma|set|reset|call|checkpoint|vacuum|use)\b",
    re.I,
)


class QueryError(ValueError):
    pass


def clean_sql(sql: str) -> str:
    """Accept one SELECT / WITH statement. The connection is read-only anyway; this gives the
    model a clear message instead of a DuckDB error."""
    text = re.sub(r"--[^\n]*", "", sql)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S).strip().rstrip(";").strip()
    if not text:
        raise QueryError("Empty query.")
    if ";" in text:
        raise QueryError("Send one statement at a time (no ';').")
    if not re.match(r"^(select|with|from)\b", text, re.I):
        raise QueryError("Only SELECT (or WITH ... SELECT) queries are allowed.")
    # Strip string literals before looking for keywords: WHERE description ILIKE '%set%' is fine
    bare = re.sub(r"'(?:[^']|'')*'", "''", text)
    if m := _FORBIDDEN.search(bare):
        raise QueryError(f"'{m.group(0)}' is not allowed: the database is read-only.")
    return text


def _plain(value):
    """DuckDB values -> JSON-safe, rounded so the page and the model see the same numbers."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 4)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()[:10]
    if isinstance(value, (int, str, bool)):
        return value
    return str(value)


def tables_in(sql: str) -> list[str]:
    low = sql.lower()
    return [t for t in TABLES if re.search(rf"\b{t}\b", low)]


def _player_ids(sql: str, params: list | None) -> list[int]:
    ids = [int(x) for x in re.findall(r"\b(?:pitcher|mlbam_id|key_mlbam)\s*(?:=|in\s*\()\s*(\d{6})", sql, re.I)]
    ids += [p for p in (params or []) if isinstance(p, int) and 100000 <= p <= 999999]
    return list(dict.fromkeys(ids))[:3]


def _bref_id(mlbam_id: int) -> str | None:
    row = connection().cursor().execute(
        "SELECT key_bbref FROM players WHERE key_mlbam = ?", [mlbam_id]).fetchone()
    return row[0] if row and row[0] else None


def sources_for(tables: list[str], player_ids: list[int]) -> list[dict]:
    """Upstream sources behind a query, with a link a reader can follow to check it."""
    out = []
    for key in dict.fromkeys(SOURCE_OF[t] for t in tables):
        if key == "internal":
            continue
        links = []
        for pid in player_ids:
            if key == "statcast":
                links.append(f"https://baseballsavant.mlb.com/savant-player/{pid}")
            elif key == "mlb_api":
                links.append(f"https://www.mlb.com/player/{pid}")
            elif key == "bref" and (bid := _bref_id(pid)):
                links.append(f"https://www.baseball-reference.com/players/{bid[0]}/{bid}.shtml")
        if not links:
            links = [{
                "statcast": "https://baseballsavant.mlb.com/statcast_search",
                "bref": "https://www.baseball-reference.com/leagues/majors/2026-standard-pitching.shtml",
                "mlb_api": "https://statsapi.mlb.com/api/v1/transactions",
                "chadwick": "https://github.com/chadwickbureau/register",
            }[key]]
        out.append({"key": key, "label": SOURCE_LABEL[key], "url": links[0]})
    return out


# --- Errors the model can fix in one try ---

_columns: dict[str, list[str]] | None = None


def _table_columns() -> dict[str, list[str]]:
    global _columns
    if _columns is None:
        rows = connection().cursor().execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'main'").fetchall()
        _columns = {}
        for t, c in rows:
            _columns.setdefault(t, []).append(c)
    return _columns


def explain_error(e: duckdb.Error, sql: str) -> str:
    """DuckDB's message (minus the echo of our wrapped SQL), plus a hint for the common mistakes:
    a column missing from GROUP BY, a column that lives in another table, a misspelled table."""
    lines = [ln.strip() for ln in str(e).splitlines() if ln.strip() and not ln.startswith("LINE ")
             and not set(ln.strip()) <= {"^"}]
    msg = f"{type(e).__name__}: " + " ".join(lines[:3])[:500]
    text = str(e)
    hint = ""
    if m := re.search(r'column "([^"]+)" must appear in the GROUP BY', text):
        hint = (f"Every SELECT column that is not inside an aggregate (count, avg, sum, ...) must be in GROUP BY. "
                f"Add {m.group(1)} to GROUP BY, or remove it from SELECT.")
    elif m := re.search(r'Referenced column "([^"]+)" not found|does not have a column named "([^"]+)"', text):
        col = (m.group(1) or m.group(2)).split(".")[-1].lower()
        cols = _table_columns()
        used = tables_in(sql) or list(cols)
        homes = [t for t, cs in cols.items() if col in cs and t != "data_dictionary"]
        close = difflib.get_close_matches(col, sorted({c for t in used for c in cols.get(t, [])}), n=4, cutoff=0.5)
        bits = []
        if homes:
            bits.append(f"{col} is a column of {', '.join(homes)}, not of {', '.join(used)}.")
        if close:
            bits.append(f"Similar columns in {', '.join(used)}: {', '.join(close)}.")
        if col == "name" and "pitches" in used:
            bits.append("In pitches the pitcher's name is player_name ('Last, First'); filter by pitcher id instead.")
        if col in ("batters_faced", "k", "bb", "k_pct", "bb_pct") and "pitches" in used:
            bits.append("In pitches, count plate appearances with events IS NOT NULL, or use pitcher_season / pitcher_game.")
        hint = " ".join(bits)
    elif "Table with name" in text:
        hint = "Tables: " + ", ".join(t for t in TABLES if t != "data_dictionary") + "."
    elif isinstance(e, duckdb.ConversionException):
        hint = "A value has the wrong type: compare numbers to numbers, and cast dates with '2026-05-01'::DATE."
    elif isinstance(e, duckdb.ParserException):
        hint = "Check the SQL syntax (DuckDB dialect). Send one complete SELECT statement."
    return msg + (f"\nHint: {hint}" if hint else "")


def run_query(ledger: Ledger, by: str, purpose: str, sql: str, params: list | None = None,
              max_rows: int = MAX_ROWS) -> Evidence:
    """Run one read-only query and record it in the ledger. Raises QueryError with a message the
    model can act on."""
    text = clean_sql(sql)
    cur = connection().cursor()
    timer = threading.Timer(QUERY_TIMEOUT_S, cur.interrupt)
    started = dt.datetime.now()
    timer.start()
    try:
        cur.execute(f"SELECT * FROM ({text}) AS q LIMIT {max_rows + 1}", params or [])
        columns = [d[0] for d in cur.description]
        raw = cur.fetchall()
    except duckdb.InterruptException:
        raise QueryError(f"Query took over {QUERY_TIMEOUT_S}s and was stopped. Use a summary table "
                         "(pitcher_season, pitcher_month, ...) or filter pitches by pitcher first.")
    except duckdb.Error as e:
        raise QueryError(explain_error(e, text))
    finally:
        timer.cancel()
        cur.close()
    rows = [[_plain(v) for v in r] for r in raw[:max_rows]]
    tables = tables_in(text)
    ev = Evidence(
        id=ledger.next_id(), by=by, purpose=purpose.strip()[:200] or "query", sql=text,
        columns=columns, rows=rows, truncated=len(raw) > max_rows, tables=tables,
        sources=sources_for(tables, _player_ids(text, params)),
        elapsed_ms=int((dt.datetime.now() - started).total_seconds() * 1000),
    )
    ledger.put(ev)
    return ev


def as_markdown(ev: Evidence) -> str:
    """What the model sees: the citation id first, then the rows."""
    src = ", ".join(s["label"] for s in ev.sources) or "database"
    head = f"[{ev.id}] {ev.purpose}  (source: {src}; cite as [{ev.id}])"
    if not ev.rows:
        return head + "\n(no rows)"
    lines = ["| " + " | ".join(ev.columns) + " |", "|" + "---|" * len(ev.columns)]
    for r in ev.rows:
        lines.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
    more = f"\n(only the first {len(ev.rows)} rows shown)" if ev.truncated else ""
    return head + "\n" + "\n".join(lines) + more
