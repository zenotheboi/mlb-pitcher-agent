"""Failed SQL gets a hint and doesn't use up the query budget; failures have their own cap."""

from types import SimpleNamespace

import pytest

from agent import tools
from agent.evidence import DB_PATH, Ledger, QueryError, run_query

pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason="data/baseball.duckdb not downloaded")

BAD = "SELECT game_year, pitch_type, avg(release_speed) FROM pitches WHERE pitcher = 694973 GROUP BY pitch_type"
GOOD = "SELECT season, k FROM pitcher_season WHERE pitcher = 694973"


def ctx():
    events = []
    run = tools.RunState(emit=events.append)
    return SimpleNamespace(context=tools.Scope(run, "Researcher 1")), events


def error_for(sql: str) -> str:
    with pytest.raises(QueryError) as e:
        run_query(Ledger(), "t", "x", sql)
    return str(e.value)


def test_group_by_hint_names_the_column():
    assert "Add game_year to GROUP BY" in error_for(BAD)


def test_missing_column_hint_says_which_table_has_it():
    msg = error_for("SELECT batters_faced FROM pitches WHERE pitcher = 694973")
    assert "batters_faced is a column of" in msg and "pitcher_season" in msg


def test_alias_form_of_missing_column_gets_a_hint_too():
    msg = error_for("SELECT p.name FROM pitches p WHERE p.pitcher = 694973")
    assert "player_name" in msg and "Hint:" in msg


def test_misspelled_column_gets_close_matches():
    assert "avg_velo" in error_for("SELECT velo FROM pitcher_season_pitch_type")


def test_failed_queries_dont_use_the_budget():
    c, events = ctx()
    out = tools._query(c, "bad", BAD)
    assert out.startswith("Query failed") and "does not count" in out
    assert c.context.queries == 0 and c.context.failed == 1
    assert events[-1]["type"] == "query_error" and "Hint:" in events[-1]["error"]
    assert "[Q1]" in tools._query(c, "good", GOOD) and c.context.queries == 1
    assert len(c.context.run.failed_queries) == 1


def test_failures_have_their_own_cap():
    c, _ = ctx()
    for _ in range(tools.MAX_FAILED_PER_AGENT):
        tools._query(c, "bad", BAD)
    assert tools._query(c, "good", GOOD).startswith("Too many failed queries")


def test_successful_queries_still_capped():
    c, _ = ctx()
    for _ in range(tools.MAX_QUERIES_PER_AGENT):
        tools._query(c, "good", GOOD)
    assert tools._query(c, "good", GOOD).startswith("Query budget used up")
