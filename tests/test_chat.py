"""/chat: Project 1's reply shape, every agent's tool calls with an `agent` field, and a clarification
that carries over to the next message. Fake agents, no model calls."""

import json
from types import SimpleNamespace

import pytest

from agent import pipeline
from agent.evidence import DB_PATH

pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason="needs data/baseball.duckdb")

CLARIFY = json.dumps({"status": "clarify", "question": "Is Smith for real?", "message": "Which Smith?",
                      "clarifying_questions": [{"question": "Which Smith?", "options": ["Cade Smith", "Will Smith"]}]})
CLEAR = json.dumps({"status": "clear", "question": "Is Cade Smith for real?"})


def tool_call(kwargs, agent, name, args, result):
    """What the SDK does after a function tool returns: call the run's hooks."""
    return kwargs["hooks"].on_tool_end(SimpleNamespace(tool_arguments=json.dumps(args)), agent,
                                       SimpleNamespace(name=name), result)


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    import app

    prompts = []

    async def run(agent, prompt, **kwargs):
        if agent.name == "Clarifier":
            prompts.append(prompt)
            return SimpleNamespace(final_output=CLEAR if "already answered" in prompt else CLARIFY)
        if agent.name == "Lead":
            await tool_call(kwargs, agent, "research", {"task": "K% change", "focus": "stuff"}, "Researcher 1 findings")
            await pipeline.spawn_researcher(kwargs["context"].run, kwargs["hooks"], "K% change", "stuff", "", 1)
            return SimpleNamespace(final_output="The brief.", to_input_list=list)
        if agent.name.startswith("Researcher"):
            await tool_call(kwargs, agent, "pitcher_overview", {"mlbam_id": 671922}, "[Q1] season lines")
            return SimpleNamespace(final_output="FINDINGS:\n- A finding.\nGAPS: none")
        if agent.name.startswith("Critic"):
            return SimpleNamespace(final_output=pipeline.CriticReview())
        if agent.name == "Writer (revise)":
            return SimpleNamespace(final_output="[]")
        return SimpleNamespace(final_output="A report.")

    monkeypatch.setattr(pipeline.Runner, "run", run)
    app.chats.clear()
    c = TestClient(app.app)
    c.prompts = prompts
    return c


def test_a_vague_question_asks_back_then_the_answer_continues_it(client):
    first = client.post("/chat", json={"message": "Is Smith for real?"}).json()
    assert first["status"] == "clarify" and "Cade Smith" in first["response"]
    assert first["clarifying_questions"][0]["options"] == ["Cade Smith", "Will Smith"]

    second = client.post("/chat", json={"message": "Cade Smith", "session_id": first["session_id"]}).json()
    assert second["status"] == "done" and second["response"] == "A report."
    assert "Is Smith for real?" in client.prompts[-1] and "Which Smith?: Cade Smith" in client.prompts[-1]


def test_tool_calls_name_the_agent_that_made_them(client):
    reply = client.post("/chat", json={"message": "Is Smith for real?", "force": True}).json()
    assert set(reply) >= {"response", "session_id", "tool_calls"}
    calls = [(c["agent"], c["name"]) for c in reply["tool_calls"]]
    assert ("Lead", "research") in calls and ("Researcher 1", "pitcher_overview") in calls
    researcher_call = next(c for c in reply["tool_calls"] if c["agent"] == "Researcher 1")
    assert researcher_call["args"] == {"mlbam_id": 671922} and researcher_call["result"] == "[Q1] season lines"


def test_new_session_drops_a_pending_clarification(client):
    first = client.post("/chat", json={"message": "Is Smith for real?"}).json()
    again = client.post("/chat", json={"message": "Is Smith for real?", "session_id": first["session_id"],
                                       "new_session": True}).json()
    assert again["status"] == "clarify" and "already answered" not in client.prompts[-1]


def test_picking_a_suggestion_after_out_of_scope_researches_it(client, monkeypatch):
    out = json.dumps({"status": "out_of_scope", "question": "Skubal's Stuff+ in 2026", "message": "No Stuff+ here.",
                      "clarifying_questions": [{"question": "Look at his pitch quality instead?",
                                                "options": ["Compare his 2026 pitch metrics to 2025"]}]})
    real = pipeline.Runner.run

    async def run(agent, prompt, **kwargs):
        if agent.name == "Clarifier" and "already answered" not in prompt:
            return SimpleNamespace(final_output=out)
        return await real(agent, prompt, **kwargs)

    monkeypatch.setattr(pipeline.Runner, "run", run)
    first = client.post("/chat", json={"message": "What was Tarik Skubal's Stuff+ in 2026?"}).json()
    assert first["status"] == "out_of_scope" and "Reply with one of these" in first["response"]
    second = client.post("/chat", json={"message": "Compare his 2026 pitch metrics to 2025",
                                        "session_id": first["session_id"]}).json()
    assert second["status"] == "done"
