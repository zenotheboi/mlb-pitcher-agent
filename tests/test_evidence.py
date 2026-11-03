"""The SQL guard and the evidence ledger, against the real database (skipped if it isn't there)."""

import pytest

from agent.evidence import DB_PATH, Ledger, QueryError, clean_sql, run_query


@pytest.mark.parametrize("sql", ["DROP TABLE pitches", "select 1; select 2", "COPY pitches TO 'x.csv'",
                                 "INSERT INTO players VALUES (1)", "ATTACH 'x.db'", "", "SET threads=1"])
def test_rejects_anything_but_one_select(sql):
    with pytest.raises(QueryError):
        clean_sql(sql)


def test_keywords_inside_strings_are_fine():
    assert clean_sql("SELECT count(*) FROM pitches WHERE description ILIKE '%set%';")


needs_db = pytest.mark.skipif(not DB_PATH.exists(), reason="data/baseball.duckdb not downloaded")


@needs_db
def test_query_gets_an_id_rows_and_a_source_link():
    led = Ledger()
    ev = run_query(led, "test", "Skenes seasons", "SELECT season, k FROM pitcher_season WHERE pitcher = 694973 ORDER BY season")
    assert ev.id == "Q1" and led.get("Q1") is ev
    assert ev.rows[0] == [2024, 170]  # matches Baseball Savant: Skenes 2024, 170 K
    assert ev.sources[0]["url"].endswith("/694973")


@needs_db
def test_files_and_urls_are_off_limits():
    with pytest.raises(QueryError):
        run_query(Ledger(), "test", "x", "SELECT * FROM read_csv('/etc/passwd')")


@needs_db
def test_results_are_capped():
    ev = run_query(Ledger(), "test", "many", "SELECT pitcher FROM pitcher_season")
    assert len(ev.rows) == 60 and ev.truncated
