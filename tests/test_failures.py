"""A failed agent gets one retry, then the run carries on with what it has. No model calls."""

import asyncio
from types import SimpleNamespace

import pytest

from agent import pipeline
from agent.evidence import DB_PATH
from context_panel import AgentContexts

GOOD_FINDINGS = "FINDINGS:\n- A finding.\nGAPS: none"


def fake_runner(monkeypatch, fail=(), times=None, draft="A report.", researchers=1):
    """Stands in for the SDK's Runner: each agent answers, except those named in `fail`, which raise
    (every time, or the first `times` calls)."""
    calls = []

    async def run(agent, prompt, **kwargs):
        calls.append(agent.name)
        failures = sum(1 for name in calls if name == agent.name)
        if any(agent.name.startswith(name) for name in fail) and (times is None or failures <= times):
            raise RuntimeError(f"{agent.name} broke")
        if agent.name == "Lead":
            for _ in range(researchers):
                await pipeline.spawn_researcher(kwargs["context"].run, kwargs["hooks"], "a task", "stuff", "", 1)
            return SimpleNamespace(final_output="The brief.", to_input_list=list)
        if agent.name.startswith("Researcher"):
            return SimpleNamespace(final_output=GOOD_FINDINGS)
        if agent.name == "Clarifier":
            return SimpleNamespace(final_output='{"status": "clear", "question": "Why?"}')
        if agent.name == "Writer (revise)":
            return SimpleNamespace(final_output="[]")
        return SimpleNamespace(final_output=draft)

    monkeypatch.setattr(pipeline.Runner, "run", run)
    return calls


def research(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    return asyncio.run(pipeline.deep_research("Why?", [], AgentContexts(), lambda event: None))


def test_a_clean_run_needs_no_fallback(tmp_path, monkeypatch):
    fake_runner(monkeypatch)
    result = research(tmp_path, monkeypatch)
    assert result["markdown"] == "A report." and result["stats"]["fallbacks"] == []


@pytest.mark.parametrize("agent", ["Clarifier", "Researcher 1", "Writer"])
def test_an_agent_recovers_from_one_failure(tmp_path, monkeypatch, agent):
    calls = fake_runner(monkeypatch, fail=[agent], times=1)
    result = research(tmp_path, monkeypatch)
    assert result["stats"]["fallbacks"] == [] and calls.count(agent) == 2


def test_a_researcher_that_keeps_failing_doesnt_sink_the_run(tmp_path, monkeypatch):
    calls = fake_runner(monkeypatch, fail=["Researcher 1"], researchers=2)
    result = research(tmp_path, monkeypatch)
    assert result["type"] == "done" and calls.count("Researcher 1") == 2
    assert [f["status"] for f in result["findings"]] == ["failed", "done"]


def test_the_run_stops_if_no_researcher_finished(tmp_path, monkeypatch):
    calls = fake_runner(monkeypatch, fail=["Researcher"])
    with pytest.raises(RuntimeError, match="nothing to write"):
        research(tmp_path, monkeypatch)
    assert "Writer" not in calls


def test_the_clarifier_failing_still_researches_the_question(tmp_path, monkeypatch):
    fake_runner(monkeypatch, fail=["Clarifier"])
    result = research(tmp_path, monkeypatch)
    assert result["type"] == "done" and result["stats"]["fallbacks"] == ["Clarifier"]


def test_the_lead_failing_keeps_whats_already_been_found(tmp_path, monkeypatch):
    calls = fake_runner(monkeypatch, fail=["Lead"])
    result = research(tmp_path, monkeypatch)
    assert result["stats"]["fallbacks"] == ["Lead"] and result["markdown"] == "A report."
    assert any(name.startswith("Researcher") for name in calls)  # the default round ran


def test_the_writer_failing_shows_the_findings(tmp_path, monkeypatch):
    fake_runner(monkeypatch, fail=["Writer"])
    result = research(tmp_path, monkeypatch)
    assert result["type"] == "done" and "A finding." in result["markdown"]
    assert "unedited" in result["markdown"] and result["stats"]["fallbacks"] == ["Writer"]


def test_the_fix_step_failing_keeps_the_finished_report(tmp_path, monkeypatch):
    # The report cites a query that never ran, so the citation check sends it back for a fix
    draft = "He threw 96.8 mph [Q9]."
    calls = fake_runner(monkeypatch, fail=["Writer (revise)"], draft=draft)
    result = research(tmp_path, monkeypatch)
    assert result["markdown"] == draft and result["stats"]["fallbacks"] == ["Writer (revise)"]
    assert calls.count("Writer (revise)") == 1  # optional, so it isn't retried


@pytest.mark.skipif(not DB_PATH.exists(), reason="data/baseball.duckdb not downloaded")
def test_the_writer_and_lead_get_the_chance_check(tmp_path, monkeypatch):
    seen = {}
    fake_runner(monkeypatch)
    fake = pipeline.Runner.run

    async def run(agent, prompt, **kwargs):
        seen[agent.name] = prompt
        return await fake(agent, prompt, **kwargs)

    async def clarify(*args):
        return pipeline.Clarification(status="clear", question="Why?",
                                      pitchers=[pipeline.PitcherRef(name="Paul Skenes", mlbam_id=694973)])

    monkeypatch.setattr(pipeline.Runner, "run", run)
    monkeypatch.setattr(pipeline, "clarify", clarify)
    result = research(tmp_path, monkeypatch)
    assert "[Q1]" in seen["Lead"] and "[Q1]" in seen["Writer"] and "Q1" in result["evidence"]


def test_tests_cant_reach_a_real_model(model_calls):
    with pytest.raises(RuntimeError, match="must not call a real model"):
        asyncio.run(pipeline.Runner.run(pipeline.writer, "Hello"))
    assert model_calls  # the block caught it, through the same path the app uses
    model_calls.clear()


def test_stopping_a_run_is_never_retried():
    calls = []

    async def stopped():
        calls.append(1)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pipeline.retried("x", stopped))
    assert len(calls) == 1
