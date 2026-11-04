"""A failed agent gets one retry, then the run carries on with what it has. No model calls."""

import asyncio
from types import SimpleNamespace

import pytest

from agents import GuardrailFunctionOutput, InputGuardrailResult
from agents.exceptions import InputGuardrailTripwireTriggered

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
        if agent.name.startswith("Critic"):
            return SimpleNamespace(final_output=pipeline.CriticReview())
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
    assert draft in result["markdown"] and "Writer (revise)" in result["stats"]["fallbacks"]
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


def tripwire(message):
    result = InputGuardrailResult(guardrail=pipeline.on_topic,
                                  output=GuardrailFunctionOutput(pipeline.TopicCheck(on_topic=False, message=message), True))
    return InputGuardrailTripwireTriggered(result)


def research_with(monkeypatch, tmp_path, clarify, force=False, answers=()):
    fake_runner(monkeypatch)
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(pipeline, "clarify", clarify)
    return asyncio.run(pipeline.deep_research("Why?", list(answers), AgentContexts(), lambda event: None, force=force))


def answer(**fields):
    async def clarify(*args):
        return pipeline.Clarification(question="Why?", **fields)
    return clarify


@pytest.mark.parametrize("status", ["off_topic", "out_of_scope"])
def test_force_cant_override_what_cant_be_researched(tmp_path, monkeypatch, status):
    assert research_with(monkeypatch, tmp_path, answer(status=status), force=True)["type"] == "clarify"


def test_force_still_researches_a_vague_question(tmp_path, monkeypatch):
    assert research_with(monkeypatch, tmp_path, answer(status="clarify"), force=True)["type"] == "done"


def test_an_off_topic_message_stops_with_the_checkers_words(monkeypatch):
    async def run(agent, prompt, **kwargs):
        raise tripwire("We research MLB pitchers.")

    monkeypatch.setattr(pipeline.Runner, "run", run)
    state = pipeline.RunState(emit=lambda event: None)
    c = asyncio.run(pipeline.clarify("Write a poem", [], 0, state, None))
    assert c.status == "off_topic" and c.message == "We research MLB pitchers."


def test_a_tripwire_isnt_retried_or_treated_as_a_failure(monkeypatch, tmp_path):
    calls = fake_runner(monkeypatch)
    fake = pipeline.Runner.run

    async def run(agent, prompt, **kwargs):
        if agent.name == "Clarifier":
            calls.append("Clarifier")
            raise tripwire("Pitchers only.")
        return await fake(agent, prompt, **kwargs)

    monkeypatch.setattr(pipeline.Runner, "run", run)
    result = research(tmp_path, monkeypatch)
    assert result["status"] == "off_topic" and calls == ["Clarifier"]


@pytest.mark.parametrize("on_topic, tripped", [(True, False), (False, True)])
def test_the_guardrail_follows_the_check(monkeypatch, on_topic, tripped):
    async def run(agent, prompt, **kwargs):
        return SimpleNamespace(final_output=pipeline.TopicCheck(on_topic=on_topic, message="x"))

    monkeypatch.setattr(pipeline.Runner, "run", run)
    assert guard(monkeypatch).tripwire_triggered is tripped


def test_a_broken_check_lets_the_question_through(monkeypatch):
    async def run(agent, prompt, **kwargs):
        raise RuntimeError("the checker broke")

    monkeypatch.setattr(pipeline.Runner, "run", run)
    assert guard(monkeypatch).tripwire_triggered is False


def guard(monkeypatch):
    context = SimpleNamespace(context=SimpleNamespace(run=SimpleNamespace(hooks=None)))
    return asyncio.run(pipeline.on_topic.guardrail_function(context, pipeline.clarifier, "A question"))


def test_the_guardrail_blocks_before_the_clarifier_starts():
    assert pipeline.clarifier.input_guardrails == [pipeline.on_topic] and pipeline.on_topic.run_in_parallel is False


def test_limits_reach_the_lead_and_the_writer(tmp_path, monkeypatch):
    seen = {}
    fake_runner(monkeypatch)
    fake = pipeline.Runner.run

    async def run(agent, prompt, **kwargs):
        seen[agent.name] = prompt
        return await fake(agent, prompt, **kwargs)

    monkeypatch.setattr(pipeline.Runner, "run", run)
    monkeypatch.setattr(pipeline, "clarify", answer(status="clear", unanswerable=["whether his Stuff+ fell"]))
    result = research(tmp_path, monkeypatch)
    assert "whether his Stuff+ fell" in seen["Lead"] and "whether his Stuff+ fell" in seen["Writer"]
    assert result["type"] == "done"


def test_a_chosen_suggestion_goes_ahead_and_keeps_the_limit(monkeypatch):
    async def run(agent, prompt, **kwargs):
        return SimpleNamespace(final_output='{"status": "out_of_scope", "unanswerable": ["his Stuff+"]}')

    monkeypatch.setattr(pipeline.Runner, "run", run)
    state = pipeline.RunState(emit=lambda event: None)
    chosen = [{"question": "Research this instead", "answer": "His velocity"}]
    c = asyncio.run(pipeline.clarify("His Stuff+?", chosen, 1, state, None))
    assert c.status == "clear" and c.unanswerable == ["his Stuff+"]


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
