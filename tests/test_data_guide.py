"""The data guide at the top of docs/schema.md is what agents trust, so its claims are checked here."""

import re

import pytest

from agent import prompts
from agent.evidence import DB_PATH, Ledger, _table_columns, connection, run_query, tables_in

pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason="data/baseball.duckdb not downloaded")

GUIDE = prompts.SCHEMA.split("## Finding the right table")[1].split("## Join paths")[0]


def one(sql):
    return run_query(Ledger(), "test", "guide", sql).rows


def test_the_schema_the_researchers_get_includes_the_guide():
    assert "## Finding the right table" in prompts.RESEARCHER and "Level of detail" in prompts.LEAD


def test_every_table_the_guide_names_exists():
    named = set(re.findall(r"`([a-z_]+)`", GUIDE.split("## Working with")[0]))
    assert set(tables_in(" ".join(named))) >= named - {"game_date", "ip_decimal", "pitcher"}


def test_pitcher_game_has_no_month_and_ip_lives_in_bref():
    cols = _table_columns()
    assert "game_month" not in cols["pitcher_game"] and "game_date" in cols["pitcher_game"]
    assert "ip_decimal" in cols["bref_pitching_standard"] and "WAR" in cols["bref_pitching_value"]


def test_summary_columns_really_are_missing_from_pitches():
    assert not {"pitches", "batters_faced", "k_pct", "xwoba"} & set(_table_columns()["pitches"])


def test_exit_velocity_really_is_on_foul_balls():
    n = connection().cursor().execute("SELECT count(launch_speed) FROM pitches WHERE type = 'S'").fetchone()[0]
    assert n > 0


def test_the_hard_hit_rule_gives_the_right_rate():
    rows = one("""SELECT round(100.0 * avg((launch_speed >= 95)::INT), 1) FROM pitches
                  WHERE pitcher = 669432 AND season = 2025 AND type = 'X' AND launch_speed IS NOT NULL""")
    assert rows == [[48.4]]  # Trevor Rogers, not the 58.1% the baseline got by counting foul balls


def test_the_home_away_rule_gives_the_right_split():
    rows = one("""SELECT CASE WHEN inning_topbot = 'Top' THEN 'home' ELSE 'away' END AS v, count(*) AS n
                  FROM pitches WHERE pitcher = 694973 AND season = 2026 GROUP BY 1 ORDER BY 1""")
    assert rows == [["away", 1223], ["home", 1667]]


def test_team_rule_gives_the_pitchers_team():
    rows = one("""SELECT DISTINCT CASE WHEN inning_topbot = 'Top' THEN home_team ELSE away_team END
                  FROM pitches WHERE pitcher = 694973 AND season = 2026""")
    assert rows == [["PIT"]]


def test_there_is_no_barrel_column():
    assert not [c for cs in _table_columns().values() for c in cs if "barrel" in c.lower()]
