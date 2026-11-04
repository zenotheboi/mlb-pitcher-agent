"""Same-pitch velocity, the chance check, and token counts by agent. No model calls."""

import asyncio
from types import SimpleNamespace

import pytest

from agent import pipeline, prompts, tools
from agent.evidence import DB_PATH, Ledger, run_query
from context_panel import AgentContexts

needs_db = pytest.mark.skipif(not DB_PATH.exists(), reason="data/baseball.duckdb not downloaded")
Y25 = ("2025-01-01", "2025-12-31")
Y26 = ("2026-01-01", "2026-12-31")


def rows(sql_and_params):
    sql, params = sql_and_params
    ev = run_query(Ledger(), "test", "x", sql, params)
    return [dict(zip(ev.columns, r)) for r in ev.rows]


def check(mlbam_id):
    return {r["stat"]: r for r in rows(tools.chance_sql(mlbam_id, *Y25, *Y26))}


@needs_db
def test_the_main_fastball_is_tracked_by_month():
    months = rows(tools.monthly_sql(554430, 2026))  # Zack Wheeler
    assert {m["main_fb"] for m in months if m["main_fb"]} == {"FF"}
    july = next(m for m in months if m["game_month"] == 7)
    assert july["main_fb_velo"] == 94.8 and july["all_fb_velo"] == 94.0  # blending hides his real fastball


@needs_db
def test_a_month_or_game_with_no_main_fastball_still_appears():
    assert len(rows(tools.monthly_sql(554430, None))) >= 3
    games = rows(tools.game_log_sql(554430, 2026, "2026-05-01", "2026-05-31"))
    assert games and all(g["main_fb"] == "FF" for g in games)


@needs_db
def test_a_real_change_has_a_large_z():
    williams = check(668909)
    assert williams["k_pct"]["z"] == pytest.approx(3.6, abs=0.3)
    assert round(williams["k_pct"]["period_b"] * 100, 1) == 33.2


@needs_db
def test_noise_has_a_small_z_even_when_the_results_moved():
    rogers = check(669432)  # ERA 1.81 to 4.03
    assert abs(rogers["k_pct"]["z"]) < 2 and abs(rogers["xwoba"]["z"]) < 2


@needs_db
def test_the_tool_gives_numbers_not_verdicts():
    assert set(check(694973)["k_pct"]) == {"stat", "period_a", "period_b", "change", "n_a", "n_b", "z"}


@needs_db
def test_the_values_match_the_season_tables():
    skenes = check(694973)
    assert round(skenes["k_pct"]["period_a"], 4) == 0.2947
    assert round(skenes["xwoba"]["period_a"], 3) == 0.255 and round(skenes["xwoba"]["period_b"], 3) == 0.281
    assert skenes["k_pct"]["z"] == pytest.approx(-0.6, abs=0.1)


@needs_db
def test_velocity_noise_is_measured_by_game_but_the_mean_stays_per_pitch():
    velo = check(554430)["main_fb_velo"]  # Zack Wheeler
    assert (velo["n_a"], velo["n_b"]) == (24, 28)  # games, not pitches
    assert velo["z"] == pytest.approx(-4.3, abs=0.1)
    assert round(velo["period_a"], 1) == 96.1 and round(velo["period_b"], 1) == 95.2  # as pitch_arsenal reports


@needs_db
def test_every_stat_is_checked():
    assert set(check(694973)) == {name for name, _, _ in tools.CHANCE_STATS} | {"main_fb_velo"}


def test_the_writer_and_researchers_get_the_same_guide_to_z():
    assert prompts.READING_Z in prompts.WRITER and prompts.READING_Z in prompts.RESEARCHER


def test_tokens_are_counted_by_agent_with_researchers_together():
    hooks = pipeline.ResearchHooks(AgentContexts(), lambda event: None)
    usage = SimpleNamespace(input_tokens=100, output_tokens=10)
    for name in ["Researcher 1", "Researcher 2", "Lead", "Writer (revise)"]:
        hooks.count(name, usage)
    assert hooks.tokens_by_agent["Researcher"] == {"input": 200, "output": 20, "calls": 2}
    assert hooks.tokens_by_agent["Writer (revise)"]["calls"] == 1
    assert hooks.tokens == {"input": 400, "output": 40, "calls": 4}
