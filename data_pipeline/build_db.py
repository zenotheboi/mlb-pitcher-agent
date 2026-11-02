"""Step 5: Build data/baseball.duckdb from data/processed/*.parquet.

uv run python -m data_pipeline.build_db

Base tables are copied from Parquet; summary tables are built with SQL; a
data_dictionary table and docs/schema.md are generated from the result.
"""
from __future__ import annotations

import duckdb

from .common import DB_PATH, PROCESSED, log
from .dictionary import DESCRIPTIONS, SOURCES

BASE_TABLES = {
    "pitches": "pitches_*.parquet",
    "players": "players.parquet",
    "bref_pitching_standard": "bref_pitching_standard.parquet",
    "bref_pitching_value": "bref_pitching_value.parquet",
    "transactions": "transactions.parquet",
    "il_stints": "il_stints.parquet",
    "mlb_people": "mlb_people.parquet",
}

# Reusable SQL fragments. Definitions match docs/schema.md.
NAME = """coalesce(pl.name_first || ' ' || pl.name_last, mp.full_name)"""
PA = """(events IS NOT NULL AND events NOT IN
        ('caught_stealing_2b','caught_stealing_3b','caught_stealing_home',
         'pickoff_1b','pickoff_2b','pickoff_3b',
         'pickoff_caught_stealing_2b','pickoff_caught_stealing_3b','pickoff_caught_stealing_home',
         'stolen_base_2b','stolen_base_3b','stolen_base_home','wild_pitch','passed_ball',
         'other_advance','truncated_pa'))"""
K = "events IN ('strikeout','strikeout_double_play')"
BB = "events IN ('walk','intent_walk')"
FB_VELO = "CASE WHEN pitch_type IN ('FF','SI','FC') THEN release_speed END"
XWOBA_NUM = """CASE WHEN woba_denom = 1 THEN
        CASE WHEN type = 'X' THEN coalesce(estimated_woba_using_speedangle, woba_value)
             ELSE woba_value END END"""


def rate(num: str, den: str) -> str:
    return f"CAST({num} AS DOUBLE) / nullif({den}, 0)"


METRICS = f"""
    count(*)                                                     AS pitches,
    count(*) FILTER (WHERE {PA})                                 AS batters_faced,
    count(*) FILTER (WHERE {K})                                  AS k,
    count(*) FILTER (WHERE {BB})                                 AS bb,
    {rate(f"count(*) FILTER (WHERE {K})", f"count(*) FILTER (WHERE {PA})")}  AS k_pct,
    {rate(f"count(*) FILTER (WHERE {BB})", f"count(*) FILTER (WHERE {PA})")} AS bb_pct,
    count(*) FILTER (WHERE events = 'home_run')                  AS hr,
    {rate("sum(is_whiff::INT)", "sum(is_swing::INT)")}           AS whiff_pct,
    {rate("sum(is_chase::INT)", "count(*) FILTER (WHERE zone BETWEEN 11 AND 14)")} AS chase_pct,
    {rate("sum(in_zone::INT)", "count(*)")}                      AS zone_pct,
    {rate("sum(is_csw::INT)", "count(*)")}                       AS csw_pct,
    avg({FB_VELO})                                               AS avg_velo_fastball,
    {rate("sum(woba_value) FILTER (WHERE woba_denom = 1)", "sum(woba_denom)")} AS woba,
    {rate(f"sum({XWOBA_NUM})", "sum(woba_denom)")}               AS xwoba,
    {rate("sum(babip_value) FILTER (WHERE type = 'X' AND events <> 'home_run')",
          "count(*) FILTER (WHERE type = 'X' AND events <> 'home_run' AND babip_value IS NOT NULL)")} AS babip,
    {rate("count(*) FILTER (WHERE events = 'home_run')",
          "count(*) FILTER (WHERE bb_type = 'fly_ball')")}       AS hr_per_fb,
    -sum(delta_run_exp)                                          AS run_value_total
"""

SUMMARY_SQL = {
    "pitcher_season": f"""
        CREATE TABLE pitcher_season AS
        WITH s AS (SELECT pitcher, season, {METRICS} FROM pitches GROUP BY pitcher, season)
        SELECT s.pitcher, s.season, {NAME} AS name, s.* EXCLUDE (pitcher, season),
               s.woba - s.xwoba AS woba_minus_xwoba
        FROM s
        LEFT JOIN players pl ON pl.key_mlbam = s.pitcher
        LEFT JOIN mlb_people mp ON mp.mlbam_id = s.pitcher
        ORDER BY s.season, s.pitches DESC""",
    "pitcher_season_pitch_type": f"""
        CREATE TABLE pitcher_season_pitch_type AS
        WITH s AS (
            SELECT pitcher, season, pitch_type,
                   count(*) AS pitches,
                   avg(release_speed) AS avg_velo, avg(release_spin_rate) AS avg_spin,
                   avg(pfx_x_in) AS avg_pfx_x_in, avg(pfx_z_in) AS avg_pfx_z_in,
                   avg(release_extension) AS avg_extension, avg(arm_angle) AS avg_arm_angle,
                   {rate("sum(is_whiff::INT)", "sum(is_swing::INT)")} AS whiff_pct,
                   {rate("sum(is_chase::INT)", "count(*) FILTER (WHERE zone BETWEEN 11 AND 14)")} AS chase_pct,
                   {rate("sum(is_csw::INT)", "count(*)")} AS csw_pct,
                   {rate(f"sum({XWOBA_NUM})", "sum(woba_denom)")} AS xwoba,
                   -sum(delta_run_exp) AS run_value_total
            FROM pitches WHERE pitch_type IS NOT NULL
            GROUP BY pitcher, season, pitch_type)
        SELECT s.pitcher, s.season, {NAME} AS name, s.pitch_type, s.pitches,
               s.pitches / sum(s.pitches) OVER (PARTITION BY s.pitcher, s.season) AS usage_pct,
               s.* EXCLUDE (pitcher, season, pitch_type, pitches)
        FROM s
        LEFT JOIN players pl ON pl.key_mlbam = s.pitcher
        LEFT JOIN mlb_people mp ON mp.mlbam_id = s.pitcher
        ORDER BY s.season, s.pitcher, s.pitches DESC""",
    "pitcher_month": f"""
        CREATE TABLE pitcher_month AS
        WITH s AS (
            SELECT pitcher, season, game_month,
                   count(*) AS pitches, avg({FB_VELO}) AS avg_velo_fastball,
                   {rate("sum(is_whiff::INT)", "sum(is_swing::INT)")} AS whiff_pct,
                   {rate("sum(is_csw::INT)", "count(*)")} AS csw_pct,
                   {rate(f"sum({XWOBA_NUM})", "sum(woba_denom)")} AS xwoba,
                   count(*) FILTER (WHERE {PA}) AS batters_faced,
                   {rate(f"count(*) FILTER (WHERE {K})", f"count(*) FILTER (WHERE {PA})")} AS k_pct,
                   {rate(f"count(*) FILTER (WHERE {BB})", f"count(*) FILTER (WHERE {PA})")} AS bb_pct
            FROM pitches GROUP BY pitcher, season, game_month)
        SELECT s.pitcher, s.season, {NAME} AS name, s.* EXCLUDE (pitcher, season)
        FROM s
        LEFT JOIN players pl ON pl.key_mlbam = s.pitcher
        LEFT JOIN mlb_people mp ON mp.mlbam_id = s.pitcher
        ORDER BY s.pitcher, s.season, s.game_month""",
    "pitcher_game": f"""
        CREATE TABLE pitcher_game AS
        WITH s AS (
            SELECT pitcher, season, game_pk, min(game_date) AS game_date,
                   count(*) AS pitches, count(*) FILTER (WHERE {PA}) AS batters_faced,
                   avg({FB_VELO}) AS avg_velo_fastball, max(release_speed) AS max_velo,
                   sum(is_whiff::INT) AS whiffs,
                   count(*) FILTER (WHERE {K}) AS k, count(*) FILTER (WHERE {BB}) AS bb
            FROM pitches GROUP BY pitcher, season, game_pk)
        SELECT s.pitcher, s.season, {NAME} AS name, s.game_pk, s.game_date,
               s.* EXCLUDE (pitcher, season, game_pk, game_date),
               date_diff('day', lag(s.game_date) OVER w, s.game_date) AS days_since_last_game
        FROM s
        LEFT JOIN players pl ON pl.key_mlbam = s.pitcher
        LEFT JOIN mlb_people mp ON mp.mlbam_id = s.pitcher
        WINDOW w AS (PARTITION BY s.pitcher, s.season ORDER BY s.game_date, s.game_pk)
        ORDER BY s.pitcher, s.game_date""",
}

EMPTY_FALLBACK = {
    # so the summary SQL still runs if an optional source has not been fetched yet
    "mlb_people": "CREATE TABLE mlb_people (mlbam_id INTEGER, full_name VARCHAR, birth_date DATE,"
                  " pitch_hand VARCHAR, primary_position VARCHAR, mlb_debut_date DATE)",
}


def build(db_path=DB_PATH) -> None:
    tmp = db_path.with_suffix(".duckdb.tmp")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    for table, pattern in BASE_TABLES.items():
        files = sorted(PROCESSED.glob(pattern))
        if not files:
            if table in EMPTY_FALLBACK:
                log.warning("%s: no parquet found; creating empty table", table)
                con.execute(EMPTY_FALLBACK[table])
                continue
            log.warning("%s: no parquet found (%s); table skipped", table, pattern)
            continue
        paths = [str(f) for f in files]
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_parquet(?, union_by_name=true)",
                    [paths])
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        log.info("%-24s %10d rows  (%d file%s)", table, n, len(files), "s" * (len(files) > 1))

    have = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    if "pitches" in have and "players" in have:
        for table, sql in SUMMARY_SQL.items():
            con.execute(sql)
            n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            log.info("%-24s %10d rows  (summary)", table, n)
    else:
        log.warning("pitches or players missing; summary tables skipped")

    build_dictionary(con)
    con.execute("CHECKPOINT")
    con.close()
    tmp.replace(db_path)
    log.info("wrote %s (%.1f MB)", db_path, db_path.stat().st_size / 1e6)

    from .schema_doc import write_schema_doc
    write_schema_doc(db_path)


def build_dictionary(con) -> None:
    cols = con.execute("""
        SELECT table_name, column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'main' ORDER BY table_name, ordinal_position""").fetchall()
    rows = []
    for t, c, dtype in cols:
        desc = DESCRIPTIONS.get((t, c)) or DESCRIPTIONS.get(("*", c)) or ""
        if not desc:
            log.debug("no description for %s.%s", t, c)
        rows.append((t, c, dtype, desc, SOURCES.get(t, "")))
    con.execute("CREATE TABLE data_dictionary (table_name VARCHAR, column_name VARCHAR, "
                "data_type VARCHAR, description VARCHAR, source VARCHAR)")
    con.executemany("INSERT INTO data_dictionary VALUES (?, ?, ?, ?, ?)", rows)
    missing = [f"{t}.{c}" for t, c, _, d, _ in rows if not d]
    if missing:
        log.warning("%d columns have no description: %s", len(missing), missing[:15])


def main() -> None:
    build()
    for f in sorted(PROCESSED.glob("*.parquet")):
        log.info("size %-36s %8.1f MB", f.name, f.stat().st_size / 1e6)


if __name__ == "__main__":
    main()
