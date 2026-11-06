"""A follow-up continues a conversation: the agents get the earlier reports, and citations keep counting
up from them. Researchers stay in fresh contexts. Fake agents, no model calls."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent import pipeline
from agent.evidence import DB_PATH, Evidence, Ledger, run_query
from agent.thread import MAX_REPORTS, Thread
from agent.verify import verify
from context_panel import AgentContexts

REPORT = "> **Verdict: Mostly luck** · Confidence: High · Injury overlap: No\n\nHis ERA rose to 3.76 [Q1]."


def evidence(qid: str, value: float = 3.76) -> Evidence:
    return Evidence(id=qid, by="Researcher 1", purpose="season line", sql="SELECT 1", columns=["era"], rows=[[value]],
                    truncated=False, tables=["bref_pitching_standard"], sources=[], elapsed_ms=1)


def earlier_thread() -> Thread:
    ledger = Ledger()
    ledger.put(evidence(ledger.next_id()))
    thread = Thread()
    thread.remember("Why did Gavin Williams's ERA rise in 2026?", REPORT, {668909: "Gavin Williams"}, ledger)
    return thread


def fake(monkeypatch, researchers=1, clarifier='{"status": "clear", "question": "Was it his slider?"}', report=None):
    """Records what each agent was given. The Lead sends `researchers` Researchers."""
    seen = {}

    async def run(agent, prompt, **kwargs):
        seen.setdefault(agent.name, []).append(prompt if isinstance(prompt, str) else json.dumps(prompt, default=str))
        if agent.name == "Clarifier":
            return SimpleNamespace(final_output=clarifier)
        if agent.name == "Lead":
            for _ in range(researchers):
                await pipeline.spawn_researcher(kwargs["context"].run, kwargs["hooks"], "a task", "stuff", "", 1)
            return SimpleNamespace(final_output="The brief.", to_input_list=list)
        if agent.name.startswith("Researcher"):
            return SimpleNamespace(final_output="FINDINGS:\n- A finding.\nGAPS: none")
        if agent.name.startswith("Critic"):
            return SimpleNamespace(final_output=pipeline.CriticReview())
        if agent.name == "Writer (revise)":
            return SimpleNamespace(final_output="[]")
        return SimpleNamespace(final_output=report or "A new report.")

    monkeypatch.setattr(pipeline.Runner, "run", run)
    return seen


def research(thread, question="Was it his slider?"):
    return asyncio.run(pipeline.deep_research(question, [], AgentContexts(), lambda event: None, thread=thread))


def test_the_clarifier_lead_and_writer_see_the_earlier_report(monkeypatch):
    seen = fake(monkeypatch)
    result = research(earlier_thread())
    for agent in ("Clarifier", "Lead", "Writer"):
        assert "His ERA rose to 3.76" in seen[agent][0], agent
    assert "Gavin Williams (id 668909)" in seen["Clarifier"][0]
    assert result["followup_of"] == 1


def test_researchers_stay_in_fresh_contexts(monkeypatch):
    seen = fake(monkeypatch)
    research(earlier_thread())
    assert seen["Researcher 1"] == ["Task: a task"]


def test_a_new_question_gets_no_follow_up_text(monkeypatch):
    seen = fake(monkeypatch)
    research(Thread())
    for agent in ("Clarifier", "Lead", "Writer"):
        assert "The conversation so far" not in seen[agent][0], agent


def test_a_finished_report_joins_the_conversation(monkeypatch):
    fake(monkeypatch)
    thread = earlier_thread()
    research(thread)
    assert [t["report"] for t in thread.turns] == [REPORT, "A new report."]


@pytest.mark.parametrize("clarifier", ['{"status": "clarify", "question": "?", "clarifying_questions": [{"question": "Who?", "options": ["A", "B"]}]}',
                                       '{"status": "out_of_scope", "question": "?", "message": "No Stuff+."}'])
def test_a_clarification_leaves_the_conversation_as_it_was(monkeypatch, clarifier):
    fake(monkeypatch, clarifier=clarifier)
    thread = earlier_thread()
    assert research(thread)["type"] == "clarify"
    assert len(thread.turns) == 1


def test_a_failed_run_leaves_the_conversation_as_it_was(monkeypatch):
    async def run(agent, prompt, **kwargs):
        if agent.name == "Clarifier":
            return SimpleNamespace(final_output='{"status": "clear", "question": "Q"}')
        if agent.name == "Lead":
            await pipeline.spawn_researcher(kwargs["context"].run, kwargs["hooks"], "a task", "stuff", "", 1)
            return SimpleNamespace(final_output="The brief.", to_input_list=list)
        raise RuntimeError("broke")

    monkeypatch.setattr(pipeline.Runner, "run", run)
    thread = earlier_thread()
    with pytest.raises(RuntimeError, match="nothing to write"):
        research(thread)
    assert len(thread.turns) == 1 and list(thread.evidence) == ["Q1"]


def test_a_follow_up_can_be_answered_without_new_research(monkeypatch):
    seen = fake(monkeypatch, researchers=0)
    result = research(earlier_thread())
    assert result["type"] == "done" and not any(name.startswith("Researcher") for name in seen)
    assert "No new research" in seen["Writer"][0]


def test_a_new_question_always_gets_research(monkeypatch):
    seen = fake(monkeypatch, researchers=0)
    research(Thread())
    assert any(name.startswith("Researcher") for name in seen)  # the default round ran


def test_earlier_citations_stay_valid_and_are_checked(monkeypatch):
    fake(monkeypatch, researchers=0, report="His ERA was 3.76 [Q1].")
    result = research(earlier_thread())
    assert list(result["evidence"]) == ["Q1"] and result["stats"]["queries"] == 0
    assert result["verification"]["numbers_matched"] == result["verification"]["numbers_checked"] == 1


def test_a_wrong_number_about_earlier_evidence_is_still_caught():
    thread = earlier_thread()
    check = verify("His ERA was 4.99 [Q1].", thread.ledger())
    assert check["numbers_matched"] == 0 and check["unmatched"]


@pytest.mark.skipif(not DB_PATH.exists(), reason="needs data/baseball.duckdb")
def test_new_queries_count_up_from_the_earlier_ones():
    ledger = earlier_thread().ledger()
    assert run_query(ledger, "Researcher 1", "count", "SELECT 1 AS n").id == "Q2"


def test_a_run_doesnt_change_the_conversations_evidence():
    thread = earlier_thread()
    ledger = thread.ledger()
    ledger.put(evidence(ledger.next_id()))
    assert list(thread.evidence) == ["Q1"]


def test_ids_stay_unique_when_an_id_was_reserved_but_never_filled():
    ledger = Ledger()
    ledger.next_id()  # reserved, and the query failed before it was stored
    ledger.put(evidence(ledger.next_id()))
    thread = Thread()
    thread.remember("Q", "R", {}, ledger)
    assert thread.ledger().next_id() == "Q3" and thread.queries() == 1


def test_the_topic_check_judges_the_new_message_not_the_reports(monkeypatch):
    screened = []

    async def run(agent, prompt, **kwargs):
        if agent.name == "Topic check":
            screened.append(prompt)
            return SimpleNamespace(final_output=pipeline.TopicCheck(on_topic=True))
        context = SimpleNamespace(context=kwargs["context"])
        await pipeline.on_topic.guardrail_function(context, agent, prompt)
        return SimpleNamespace(final_output='{"status": "clear", "question": "Q"}')

    monkeypatch.setattr(pipeline.Runner, "run", run)
    thread = earlier_thread()
    state = pipeline.RunState(emit=lambda event: None, thread=thread)
    state.hooks = pipeline.ResearchHooks(AgentContexts(), lambda event: None)
    asyncio.run(pipeline.clarify("Write me a poem", [], 0, state, state.hooks))
    assert "New message:\nQuestion: Write me a poem" in screened[0]
    assert "Why did Gavin Williams's ERA rise" in screened[0] and "His ERA rose to 3.76" not in screened[0]


def test_only_recent_reports_are_read_in_full():
    thread = Thread()
    for i in range(MAX_REPORTS + 2):
        thread.remember(f"question {i}", f"report {i}", {1: "A"}, Ledger())
    context = thread.context()
    assert "report 0" not in context and "report 1" not in context and f"report {MAX_REPORTS + 1}" in context
    assert "question 0" in context  # older questions stay as one line


def test_the_clarifiers_fallback_keeps_the_conversations_pitchers(monkeypatch):
    seen = fake(monkeypatch)
    real = pipeline.Runner.run

    async def run(agent, prompt, **kwargs):
        if agent.name == "Clarifier":
            raise RuntimeError("Clarifier broke")
        return await real(agent, prompt, **kwargs)

    monkeypatch.setattr(pipeline.Runner, "run", run)
    result = research(earlier_thread())
    assert result["stats"]["fallbacks"] == ["Clarifier"]
    assert "Gavin Williams (id 668909)" in seen["Lead"][0]


# --- Through the API ---


@pytest.fixture
def client(monkeypatch):
    if not DB_PATH.exists():
        pytest.skip("needs data/baseball.duckdb")
    from fastapi.testclient import TestClient

    import app

    fake(monkeypatch)
    app.chats.clear()
    return TestClient(app.app)


def test_chat_keeps_the_conversation_for_the_same_session(client):
    first = client.post("/chat", json={"message": "Why did his ERA rise?"}).json()
    second = client.post("/chat", json={"message": "Was it his slider?", "session_id": first["session_id"]}).json()
    assert (first["followup_of"], second["followup_of"]) == (0, 1)
    assert first["session_id"] == second["session_id"]


def test_chat_new_session_and_a_new_id_start_over(client):
    first = client.post("/chat", json={"message": "Why did his ERA rise?"}).json()
    fresh = client.post("/chat", json={"message": "Next", "session_id": first["session_id"], "new_session": True}).json()
    other = client.post("/chat", json={"message": "Next"}).json()
    assert fresh["followup_of"] == 0 and other["followup_of"] == 0


def test_run_says_how_many_earlier_answers_it_continues(client):
    def start(**params):
        with client.stream("GET", "/run", params={"question": "Q", **params}) as stream:
            events = [json.loads(line[6:]) for line in stream.iter_lines() if line.startswith("data: ")]
        return events[0], events[-1]

    first, done = start()
    assert first["followup_of"] == 0 and done["type"] == "done"
    second, _ = start(session_id=first["session_id"])
    assert second["followup_of"] == 1 and second["session_id"] == first["session_id"]
    lost, _ = start(session_id="an-id-the-server-doesnt-know")
    assert lost["followup_of"] == 0
