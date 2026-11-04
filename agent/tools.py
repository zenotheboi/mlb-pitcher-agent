"""The agents' tools. Each one runs read-only SQL through evidence.run_query, so every result the
model sees carries a citation id ([Q7]) and lands in the run's ledger.

The shaped tools (pitcher_overview, pitch_arsenal, ...) answer the common questions with SQL we
wrote and checked; run_sql is there for everything else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal

from agents import RunContextWrapper, function_tool

from data_pipeline.build_db import BB, K, METRICS, PA, XWOBA_NUM

from .evidence import Ledger, QueryError, as_markdown, connection, run_query

log = logging.getLogger("pitchlab")

# One researcher's query budget: successful queries only. Past it, the tool tells the model to
# write up what it has. Failed queries don't use it up, but hand-written SQL has a failure cap of its
# own, so a researcher can't retry a broken query forever. The shaped tools keep working past it.
MAX_QUERIES_PER_AGENT = 8
MAX_FAILED_PER_AGENT = 4


@dataclass
class RunState:
    """One research run's state, shared by every agent and tool in it. The models never see it."""

    emit: Any  # callable(event: dict)
    ledger: Ledger = field(default_factory=Ledger)
    researchers: int = 0
    findings: list[dict] = field(default_factory=list)  # {"id","task","focus","wave","why","text",...}
    lead_turns: int = 0  # the Lead's model calls so far: research calls from one call form a wave
    wave_of_turn: dict[int, int] = field(default_factory=dict)
    pitchers: dict[int, str] = field(default_factory=dict)  # mlbam_id -> name, for the dashboard
    started: float = 0.0  # time.time() when the run began, for the time budget
    failed_queries: list[dict] = field(default_factory=list)  # {"agent","tool","purpose","error","sql"}
    # Every query attempt in order, for the run file: {"agent","tool","purpose","outcome"}, where
    # outcome is ok, failed, over_budget or over_failed_limit
    query_log: list[dict] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)  # phase -> seconds
    brief: str = ""  # the Lead's brief for the Writer
    draft: str = ""  # the Writer's first draft, before the citation fixes
    fallbacks: list[str] = field(default_factory=list)  # agents the run had to carry on without
    chance_check: str = ""  # the code's chance check, as the Lead and the Writer see it
    unanswerable: list[str] = field(default_factory=list)  # what the question asks that the data can't answer
    hooks: Any = None  # so a guardrail's own model call is counted too


@dataclass
class Scope:
    """The context one agent's tools run in: the shared run, and who is asking."""

    run: RunState
    agent: str
    queries: int = 0  # successful
    failed: int = 0


def _query(ctx: RunContextWrapper[Scope], purpose: str, sql: str, params: list | None = None,
           hand_written: bool = False) -> str:
    scope = ctx.context
    tool = getattr(ctx, "tool_name", "")  # the SDK's ToolContext has it

    def attempt(outcome: str) -> None:
        scope.run.query_log.append({"agent": scope.agent, "tool": tool, "purpose": purpose, "outcome": outcome})

    if scope.queries >= MAX_QUERIES_PER_AGENT:
        attempt("over_budget")
        return "Query budget used up. Write your findings now from the results you have."
    if hand_written and scope.failed >= MAX_FAILED_PER_AGENT:
        attempt("over_failed_limit")
        return ("Too many failed queries. Stop writing SQL: use the shaped tools, or write your findings now "
                "and list what you couldn't get under GAPS.")
    try:
        ev = run_query(scope.run.ledger, scope.agent, purpose, sql, params)
    except QueryError as e:
        scope.failed += 1
        attempt("failed")
        scope.run.failed_queries.append({"agent": scope.agent, "tool": tool, "purpose": purpose, "error": str(e), "sql": sql})
        log.warning("query failed | %s | %s | %s | sql: %s", scope.agent, purpose, str(e).replace("\n", " "),
                    " ".join(sql.split())[:600])
        scope.run.emit({"type": "query_error", "agent": scope.agent, "purpose": purpose, "error": str(e),
                        "failed": scope.failed, "max_failed": MAX_FAILED_PER_AGENT})
        left = MAX_FAILED_PER_AGENT - scope.failed
        return (f"Query failed (it does not count against your query budget): {e}\n"
                f"Fix the query and try again ({left} more failed queries allowed).")
    scope.queries += 1
    attempt("ok")
    # The whole record, so the page can show the SQL and rows while the run is still going
    scope.run.emit({"type": "query", "agent": scope.agent, **ev.public()})
    return as_markdown(ev)


def _remember(ctx: RunContextWrapper[Scope], mlbam_id: int) -> None:
    if mlbam_id in ctx.context.run.pitchers:
        return
    row = connection().cursor().execute(
        "SELECT any_value(name) FROM pitcher_season WHERE pitcher = ?", [mlbam_id]).fetchone()
    if row and row[0]:
        ctx.context.run.pitchers[mlbam_id] = row[0]
        ctx.context.run.emit({"type": "pitcher", "mlbam_id": mlbam_id, "name": row[0]})


# --- Finding a pitcher (not a citation: it only resolves names to ids) ---

LOOKUP_SQL = """
WITH statcast AS (
    SELECT pitcher AS mlbam_id, any_value(name) AS name,
           string_agg(DISTINCT season::VARCHAR, ',' ORDER BY season::VARCHAR) AS statcast_seasons,
           sum(pitches) AS pitches, sum(batters_faced) AS batters_faced
    FROM pitcher_season GROUP BY pitcher),
bref AS (
    SELECT mlbam_id, any_value(name) AS name,
           min(season) AS first_season, max(season) AS last_season, sum(ip_decimal) AS career_ip
    FROM bref_pitching_standard WHERE mlbam_id IS NOT NULL GROUP BY mlbam_id),
everyone AS (
    SELECT coalesce(s.mlbam_id, b.mlbam_id) AS mlbam_id, coalesce(s.name, b.name) AS name,
           s.statcast_seasons, s.pitches, s.batters_faced, b.first_season, b.last_season,
           round(b.career_ip, 1) AS bref_ip_2015_2026
    FROM statcast s FULL JOIN bref b USING (mlbam_id)),
scored AS (
    SELECT *, CASE
        WHEN lower(name) = lower($q) THEN 3
        WHEN lower(name) LIKE '%' || lower($q) || '%' THEN 2
        ELSE greatest(jaro_winkler_similarity(lower(name), lower($q)),
                      jaro_winkler_similarity(lower(regexp_extract(name, '(\\S+)$')), lower($q)))
        END AS score
    FROM everyone)
SELECT mlbam_id, name, statcast_seasons, pitches, batters_faced, first_season, last_season,
       bref_ip_2015_2026, round(score, 2) AS match
FROM scored WHERE score >= 0.88
ORDER BY score DESC, coalesce(pitches, 0) DESC LIMIT 8
"""

TEAM_SQL = """
SELECT pitcher, arg_max(CASE WHEN inning_topbot = 'Top' THEN home_team ELSE away_team END, game_date) AS team,
       any_value(p_throws) AS throws
FROM pitches WHERE pitcher IN (SELECT unnest($ids::INTEGER[])) GROUP BY pitcher
"""


def lookup_pitchers(name: str) -> list[dict]:
    cur = connection().cursor()
    rows = cur.execute(LOOKUP_SQL, {"q": name.strip()}).fetchall()
    cols = [d[0] for d in cur.description]
    found = [dict(zip(cols, r)) for r in rows]
    ids = [f["mlbam_id"] for f in found if f["pitches"]]
    if ids:
        team = {r[0]: (r[1], r[2]) for r in cur.execute(TEAM_SQL, {"ids": ids}).fetchall()}
        pos = dict(cur.execute("SELECT mlbam_id, primary_position FROM mlb_people").fetchall())
        for f in found:
            f["last_team"], f["throws"] = team.get(f["mlbam_id"], (None, None))
            f["position"] = pos.get(f["mlbam_id"])
    return found


@function_tool
def find_pitcher(ctx: RunContextWrapper[Scope], name: str) -> str:
    """Find pitchers by name (fuzzy). Returns MLBAM ids, seasons with Statcast data, last team,
    throwing hand and position. Use it to turn a name into an id; it is not a citable source.

    Args:
        name: A full or partial name, e.g. 'Skenes' or 'Will Smith'.
    """
    found = lookup_pitchers(name)
    if not found:
        return f"No pitcher matches '{name}'. Check the spelling, or try the last name only."
    lines = []
    for f in found:
        bits = [f"id {f['mlbam_id']}"]
        if f.get("throws"):
            bits.append(f"{f['throws']}HP" if f.get("position") == "P" else f"throws {f['throws']}, position {f.get('position')}")
        if f.get("last_team"):
            bits.append(f"last team {f['last_team']}")
        if f["statcast_seasons"]:
            bits.append(f"Statcast {f['statcast_seasons']} ({f['pitches']} pitches)")
        if f["first_season"]:
            bits.append(f"B-Ref {f['first_season']}-{f['last_season']} ({f['bref_ip_2015_2026']} IP)")
        lines.append(f"- {f['name']}: " + ", ".join(bits))
    return "\n".join(lines)


def main_fastball(mlbam_id: int) -> str:
    """SQL for the fastball a pitcher throws most. Tracking one pitch type keeps a change in pitch mix
    from looking like a change in velocity, which the blended avg_velo_fastball can't do."""
    return (f"(SELECT pitch_type FROM pitches WHERE pitcher = {int(mlbam_id)} AND pitch_type IN ('FF', 'SI', 'FC') "
            "GROUP BY pitch_type ORDER BY count(*) DESC LIMIT 1)")


# --- Shaped research tools: each one is one or two citable queries ---

SEASON_SQL = """
SELECT season, name, pitches, batters_faced, k_pct, bb_pct, whiff_pct, chase_pct, zone_pct, csw_pct,
       avg_velo_fastball, woba, xwoba, woba_minus_xwoba, babip, hr, hr_per_fb, run_value_total
FROM pitcher_season WHERE pitcher = ? ORDER BY season"""

BREF_SQL = """
SELECT s.season, s.team, s.age, s.G, s.GS, round(s.ip_decimal, 1) AS ip, s.ERA, s.FIP,
       round(s.ERA - s.FIP, 2) AS era_minus_fip, s.ERA_plus, s.WHIP, s.SO9, s.BB9, s.HR9, v.WAR
FROM bref_pitching_standard s
LEFT JOIN bref_pitching_value v ON v.season = s.season AND v.bref_id = s.bref_id
WHERE s.mlbam_id = ? ORDER BY s.season"""


@function_tool
def pitcher_overview(ctx: RunContextWrapper[Scope], mlbam_id: int) -> str:
    """A pitcher's season lines: Statcast 2024-2026 (K%, BB%, whiff, chase, CSW, fastball velo,
    wOBA vs xwOBA, BABIP, HR/FB, run value) and Baseball Reference 2015-2026 (ERA, FIP, ERA-FIP,
    ERA+, WHIP, IP, WAR). Two citable queries.

    Args:
        mlbam_id: The pitcher's MLBAM id (from find_pitcher).
    """
    _remember(ctx, mlbam_id)
    a = _query(ctx, f"Statcast season lines, pitcher {mlbam_id}", SEASON_SQL, [mlbam_id])
    b = _query(ctx, f"Baseball Reference season lines 2015-2026, pitcher {mlbam_id}", BREF_SQL, [mlbam_id])
    return a + "\n\n" + b


@function_tool
def pitch_arsenal(ctx: RunContextWrapper[Scope], mlbam_id: int) -> str:
    """Pitch mix and pitch traits by season and pitch type, with the change from the previous season:
    usage, velocity, spin, horizontal / induced vertical movement (inches), extension, arm angle,
    whiff%, CSW%, xwOBA and run value per pitch type.

    Args:
        mlbam_id: The pitcher's MLBAM id.
    """
    _remember(ctx, mlbam_id)
    sql = """
    SELECT season, pitch_type, pitches, round(usage_pct, 3) AS usage_pct,
           round(avg_velo, 1) AS velo, round(avg_spin) AS spin,
           round(avg_pfx_x_in, 1) AS h_break_in, round(avg_pfx_z_in, 1) AS ivb_in,
           round(avg_extension, 2) AS extension_ft, round(avg_arm_angle, 1) AS arm_angle,
           round(whiff_pct, 3) AS whiff_pct, round(csw_pct, 3) AS csw_pct, round(xwoba, 3) AS xwoba,
           round(run_value_total, 1) AS run_value,
           round(usage_pct - lag(usage_pct) OVER w, 3) AS usage_chg,
           round(avg_velo - lag(avg_velo) OVER w, 1) AS velo_chg,
           round(avg_pfx_z_in - lag(avg_pfx_z_in) OVER w, 1) AS ivb_chg,
           round(whiff_pct - lag(whiff_pct) OVER w, 3) AS whiff_chg
    FROM pitcher_season_pitch_type WHERE pitcher = ? AND pitches >= 20
    WINDOW w AS (PARTITION BY pitch_type ORDER BY season)
    ORDER BY season, pitches DESC"""
    return _query(ctx, f"Pitch arsenal by season, pitcher {mlbam_id}", sql, [mlbam_id])


def monthly_sql(mlbam_id: int, season: int | None) -> tuple[str, list]:
    sql = f"""
    SELECT m.season, m.game_month, m.pitches, m.batters_faced, f.main_fb, f.main_fb_velo,
           round(m.avg_velo_fastball, 1) AS all_fb_velo, round(m.whiff_pct, 3) AS whiff_pct,
           round(m.csw_pct, 3) AS csw_pct, round(m.k_pct, 3) AS k_pct, round(m.bb_pct, 3) AS bb_pct,
           round(m.xwoba, 3) AS xwoba
    FROM pitcher_month m
    LEFT JOIN (SELECT season, game_month, any_value(pitch_type) AS main_fb, round(avg(release_speed), 1) AS main_fb_velo
               FROM pitches WHERE pitcher = ? AND pitch_type = {main_fastball(mlbam_id)} GROUP BY season, game_month) f
           ON f.season = m.season AND f.game_month = m.game_month
    WHERE m.pitcher = ? AND (? IS NULL OR m.season = ?)
    ORDER BY m.season, m.game_month"""
    return sql, [mlbam_id, mlbam_id, season, season]


@function_tool
def monthly_trend(ctx: RunContextWrapper[Scope], mlbam_id: int, season: int | None = None) -> str:
    """Month-by-month velocity of his main fastball, whiff%, CSW%, K%, BB% and xwOBA, to see when a
    change began. main_fb_velo tracks one pitch type; all_fb_velo blends his fastballs, so it also
    moves when his pitch mix does.

    Args:
        mlbam_id: The pitcher's MLBAM id.
        season: One season (2024-2026), or omit for all three.
    """
    _remember(ctx, mlbam_id)
    return _query(ctx, f"Monthly trend, pitcher {mlbam_id}" + (f", {season}" if season else ""),
                  *monthly_sql(mlbam_id, season))


def game_log_sql(mlbam_id: int, season: int, start_date: str | None, end_date: str | None) -> tuple[str, list]:
    sql = f"""
    SELECT g.game_date, g.pitches, g.batters_faced, f.main_fb, f.main_fb_velo,
           round(g.avg_velo_fastball, 1) AS all_fb_velo, round(g.max_velo, 1) AS max_velo, g.whiffs, g.k, g.bb,
           g.days_since_last_game AS rest_days
    FROM pitcher_game g
    LEFT JOIN (SELECT game_pk, any_value(pitch_type) AS main_fb, round(avg(release_speed), 1) AS main_fb_velo
               FROM pitches WHERE pitcher = ? AND pitch_type = {main_fastball(mlbam_id)} GROUP BY game_pk) f
           ON f.game_pk = g.game_pk
    WHERE g.pitcher = ? AND g.season = ?
      AND (?::DATE IS NULL OR g.game_date >= ?::DATE) AND (?::DATE IS NULL OR g.game_date <= ?::DATE)
    ORDER BY g.game_date"""
    return sql, [mlbam_id, mlbam_id, season, start_date, start_date, end_date, end_date]


@function_tool
def game_log(ctx: RunContextWrapper[Scope], mlbam_id: int, season: int,
             start_date: str | None = None, end_date: str | None = None) -> str:
    """Game-by-game pitches, batters faced, velocity of his main fastball, max velocity, whiffs, K, BB
    and days of rest. Use it around an IL stint or a sudden change. main_fb_velo tracks one pitch
    type; all_fb_velo blends his fastballs, so it also moves when his pitch mix does.

    Args:
        mlbam_id: The pitcher's MLBAM id.
        season: Season (2024-2026).
        start_date: Optional first date, YYYY-MM-DD.
        end_date: Optional last date, YYYY-MM-DD.
    """
    _remember(ctx, mlbam_id)
    return _query(ctx, f"Game log {season}, pitcher {mlbam_id}", *game_log_sql(mlbam_id, season, start_date, end_date))


@function_tool
def injury_history(ctx: RunContextWrapper[Scope], mlbam_id: int) -> str:
    """Injured list stints 2024-2026 (dates, length, the public injury note and the original
    transaction text to quote) and other roster moves (trades, options, DFA, releases).

    Args:
        mlbam_id: The pitcher's MLBAM id.
    """
    _remember(ctx, mlbam_id)
    il = """
    SELECT season, team, il_type, start_date, end_date, days_on_il, injury_note, end_reason,
           placement_description, activation_description
    FROM il_stints WHERE mlbam_id = ? ORDER BY start_date"""
    moves = """
    SELECT date, type_desc, description FROM transactions
    WHERE mlbam_id = ? AND type_code IN ('TR', 'OPT', 'CU', 'DES', 'OUT', 'REL', 'SFA', 'SGN', 'CLW', 'SU', 'RET')
    ORDER BY date LIMIT 30"""
    a = _query(ctx, f"Injured list stints, pitcher {mlbam_id}", il, [mlbam_id])
    b = _query(ctx, f"Roster transactions, pitcher {mlbam_id}", moves, [mlbam_id])
    return a + "\n\n" + b


@function_tool
def compare_periods(ctx: RunContextWrapper[Scope], mlbam_id: int, a_start: str, a_end: str,
                    b_start: str, b_end: str) -> str:
    """Compare two date ranges for one pitcher from pitch-level data, e.g. before and after an IL
    stint or 2025 vs 2026: pitches, BF, K%, BB%, whiff%, chase%, zone%, CSW%, fastball velo (main_fb_velo
    tracks one pitch type), wOBA, xwOBA, BABIP, HR/FB, run value.

    Args:
        mlbam_id: The pitcher's MLBAM id.
        a_start: Period A first date, YYYY-MM-DD.
        a_end: Period A last date, YYYY-MM-DD.
        b_start: Period B first date, YYYY-MM-DD.
        b_end: Period B last date, YYYY-MM-DD.
    """
    _remember(ctx, mlbam_id)
    sql = f"""
    SELECT CASE WHEN game_date BETWEEN ?::DATE AND ?::DATE THEN 'A' ELSE 'B' END AS period,
           min(game_date) AS first_game, max(game_date) AS last_game, {METRICS},
           round(avg(release_speed) FILTER (WHERE pitch_type = {main_fastball(mlbam_id)}), 1) AS main_fb_velo
    FROM pitches
    WHERE pitcher = ? AND (game_date BETWEEN ?::DATE AND ?::DATE OR game_date BETWEEN ?::DATE AND ?::DATE)
    GROUP BY period ORDER BY period"""
    return _query(ctx, f"Period A {a_start}..{a_end} vs B {b_start}..{b_end}, pitcher {mlbam_id}", sql,
                  [a_start, a_end, mlbam_id, a_start, a_end, b_start, b_end])


# Each stat is an average over the pitches or batters it applies to, which is what lets one test cover
# them all: a rate is the average of 1s and 0s. (name, value, which rows count). The agents get the
# numbers and judge them: the guide is prompts.READING_Z.
CHANCE_STATS = [
    ("k_pct", K, PA), ("bb_pct", BB, PA), ("hr_per_bf", "events = 'home_run'", PA),
    ("whiff_pct", "is_whiff", "is_swing"), ("chase_pct", "is_chase", "zone BETWEEN 11 AND 14"),
    ("zone_pct", "in_zone", "TRUE"), ("csw_pct", "is_csw", "TRUE"),
    ("xwoba", XWOBA_NUM, "woba_denom = 1"), ("woba", "woba_value", "woba_denom = 1"),
    ("babip", "babip_value", "type = 'X' AND events <> 'home_run' AND babip_value IS NOT NULL"),
    ("exit_velo", "launch_speed", "type = 'X' AND launch_speed IS NOT NULL"),
]
def chance_sql(mlbam_id: int, a_start: str, a_end: str, b_start: str, b_end: str) -> tuple[str, list]:
    rows = "\n        UNION ALL ".join(f"SELECT '{name}' AS stat, period, CAST(({value}) AS DOUBLE) AS v FROM p WHERE {where}"
                                     for name, value, where in CHANCE_STATS)
    sql = f"""
    WITH p AS (SELECT *, CASE WHEN game_date BETWEEN ?::DATE AND ?::DATE THEN 'A' ELSE 'B' END AS period
               FROM pitches
               WHERE pitcher = ? AND (game_date BETWEEN ?::DATE AND ?::DATE OR game_date BETWEEN ?::DATE AND ?::DATE)),
    long AS ({rows}),
    games AS (SELECT period, game_pk, avg(release_speed) AS v, count(*) AS c FROM p
              WHERE release_speed IS NOT NULL AND pitch_type = {main_fastball(mlbam_id)} GROUP BY period, game_pk),
    s AS (SELECT stat, period, avg(v) AS mean, coalesce(var_samp(v), 0) AS variance, count(v) AS n
          FROM long GROUP BY stat, period
          UNION ALL
          -- Velocity moves with the day, so its noise is measured game to game. The mean is still per pitch.
          SELECT 'main_fb_velo', period, sum(v * c) / sum(c), coalesce(var_samp(v), 0), count(*) FROM games GROUP BY period)
    SELECT a.stat, a.mean AS period_a, b.mean AS period_b, b.mean - a.mean AS change, a.n AS n_a, b.n AS n_b,
           round((b.mean - a.mean) / nullif(sqrt(a.variance / a.n + b.variance / b.n), 0), 1) AS z
    FROM s a JOIN s b ON a.stat = b.stat AND a.period = 'A' AND b.period = 'B'
    ORDER BY abs(z) DESC NULLS LAST"""
    return sql, [a_start, a_end, mlbam_id, a_start, a_end, b_start, b_end]


def chance_checks(run: RunState, compare: dict) -> str:
    """The chance check for each pitcher in the question, run by code so the Writer always has it.
    Returns the citable results, or nothing if a check couldn't run."""
    ctx = SimpleNamespace(context=Scope(run, "Chance check"), tool_name="check_change")
    span = f"{compare['a_start']}..{compare['a_end']} vs {compare['b_start']}..{compare['b_end']}"
    results = [_query(ctx, f"Chance check, {name}, {span}", *chance_sql(mlbam_id, **compare))
               for mlbam_id, name in run.pitchers.items()]
    return "\n\n".join(r for r in results if r.startswith("[Q"))


@function_tool
def check_change(ctx: RunContextWrapper[Scope], mlbam_id: int, a_start: str, a_end: str,
                 b_start: str, b_end: str) -> str:
    """Is a change between two periods bigger than normal chance? For K%, BB%, HR rate, whiff%, chase%,
    zone%, CSW%, xwOBA, wOBA, BABIP, exit velocity and main fastball velocity, it gives both values, the
    change, the sample sizes (n_a, n_b) and z, the change measured against normal noise. Use it before
    calling any change real. For velocity, n counts games, because velocity moves with the day.

    Args:
        mlbam_id: The pitcher's MLBAM id.
        a_start: Period A first date, YYYY-MM-DD. For a season use 2025-01-01.
        a_end: Period A last date, YYYY-MM-DD.
        b_start: Period B first date, YYYY-MM-DD.
        b_end: Period B last date, YYYY-MM-DD.
    """
    _remember(ctx, mlbam_id)
    return _query(ctx, f"Chance check, {a_start}..{a_end} vs {b_start}..{b_end}, pitcher {mlbam_id}",
                  *chance_sql(mlbam_id, a_start, a_end, b_start, b_end))


LEAGUE_METRICS = ["k_pct", "bb_pct", "whiff_pct", "chase_pct", "zone_pct", "csw_pct", "avg_velo_fastball",
                  "woba", "xwoba", "woba_minus_xwoba", "babip", "hr_per_fb", "run_value_total"]


@function_tool
def league_context(ctx: RunContextWrapper[Scope], mlbam_id: int, season: int, min_batters_faced: int = 150) -> str:
    """Where a pitcher ranks among MLB pitchers that season: value, league median and percentile
    (share of qualifying pitchers with a LOWER value; for xwoba, woba, bb_pct, babip, hr_per_fb
    lower is better for the pitcher) for each metric.

    Args:
        mlbam_id: The pitcher's MLBAM id.
        season: Season (2024-2026).
        min_batters_faced: Pool: pitchers with at least this many batters faced.
    """
    _remember(ctx, mlbam_id)
    cols = ", ".join(LEAGUE_METRICS)
    sql = f"""
    WITH pool AS (SELECT pitcher, {cols} FROM pitcher_season
                  WHERE season = ? AND (batters_faced >= ? OR pitcher = ?)),
    long AS (UNPIVOT pool ON {cols} INTO NAME metric VALUE value),
    ranked AS (SELECT *, percent_rank() OVER (PARTITION BY metric ORDER BY value) AS pr,
                      median(value) OVER (PARTITION BY metric) AS med,
                      count(*) OVER (PARTITION BY metric) AS n
               FROM long)
    SELECT metric, round(value, 3) AS value, round(med, 3) AS league_median,
           round(100 * pr) AS percentile, n AS pool_size
    FROM ranked WHERE pitcher = ?"""
    return _query(ctx, f"League percentiles {season} (min {min_batters_faced} BF), pitcher {mlbam_id}", sql,
                  [season, min_batters_faced, mlbam_id, mlbam_id])


HIGHER_IS_BETTER = {"k_pct": True, "bb_pct": False, "whiff_pct": True, "chase_pct": True, "zone_pct": True,
                    "csw_pct": True, "avg_velo_fastball": True, "woba": False, "xwoba": False,
                    "babip": False, "hr_per_fb": False, "run_value_total": True}


@function_tool
def season_changes(ctx: RunContextWrapper[Scope], season: int,
                   metric: Literal["k_pct", "bb_pct", "whiff_pct", "chase_pct", "zone_pct", "csw_pct",
                                   "avg_velo_fastball", "woba", "xwoba", "babip", "hr_per_fb",
                                   "run_value_total"],
                   direction: Literal["worse", "better"] = "worse", min_batters_faced: int = 200,
                   limit: int = 15) -> str:
    """League-wide: the pitchers whose metric changed most from the previous season (both seasons
    with at least min_batters_faced). 'worse' / 'better' are from the pitcher's point of view.

    Args:
        season: The later season (2025 or 2026).
        metric: The pitcher_season column to compare.
        direction: 'worse' or 'better' for the pitcher.
        min_batters_faced: Minimum batters faced in each season.
        limit: How many pitchers (at most 30).
    """
    better_up = HIGHER_IS_BETTER[metric]
    order = "DESC" if (direction == "better") == better_up else "ASC"
    sql = f"""
    SELECT cur.pitcher AS mlbam_id, cur.name, round(prev.{metric}, 3) AS prev_value,
           round(cur.{metric}, 3) AS value, round(cur.{metric} - prev.{metric}, 3) AS change,
           prev.batters_faced AS prev_bf, cur.batters_faced AS bf,
           round(cur.xwoba - prev.xwoba, 3) AS xwoba_change,
           round(cur.avg_velo_fastball - prev.avg_velo_fastball, 1) AS fb_velo_change
    FROM pitcher_season cur JOIN pitcher_season prev ON prev.pitcher = cur.pitcher AND prev.season = cur.season - 1
    WHERE cur.season = ? AND cur.batters_faced >= ? AND prev.batters_faced >= ?
    ORDER BY change {order} NULLS LAST LIMIT {max(1, min(int(limit), 30))}"""
    return _query(ctx, f"Largest {metric} changes {season - 1}->{season} ({direction})", sql,
                  [season, min_batters_faced, min_batters_faced])


@function_tool
def run_sql(ctx: RunContextWrapper[Scope], sql: str, purpose: str) -> str:
    """Run one read-only DuckDB SELECT over the database (schema in your instructions). At most 60 rows
    come back. Aggregate in SQL, filter `pitches` by pitcher, and compute changes in SQL so the
    numbers you cite are in the result.

    Args:
        sql: One SELECT (or WITH ... SELECT) statement.
        purpose: A few words on what this query shows, e.g. 'Skenes sweeper usage by month 2026'.
    """
    return _query(ctx, purpose, sql, hand_written=True)


@function_tool
def describe_table(ctx: RunContextWrapper[Scope], table: str) -> str:
    """Columns of one table, with meaning and units (from the data dictionary).

    Args:
        table: Table name, e.g. 'pitcher_season'.
    """
    rows = connection().cursor().execute(
        "SELECT column_name, data_type, description FROM data_dictionary WHERE table_name = ?", [table]).fetchall()
    if not rows:
        return f"No table named {table}."
    return "\n".join(f"- {c} ({t}): {d}" for c, t, d in rows)


RESEARCH_TOOLS = [find_pitcher, pitcher_overview, pitch_arsenal, monthly_trend, game_log, injury_history,
                  compare_periods, check_change, league_context, season_changes, run_sql, describe_table]
