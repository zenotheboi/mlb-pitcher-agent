"""The Writer's draft reaches the page as it's written. Fake agents, no model calls."""

import asyncio
from types import SimpleNamespace

from agent import pipeline
from context_panel import AgentContexts
from tests.test_failures import fake_runner


class TextStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.final_output = "".join(chunks)

    async def stream_events(self):
        for c in self.chunks:
            yield SimpleNamespace(type="raw_response_event", data=SimpleNamespace(type="response.output_text.delta", delta=c))


def test_the_writer_streams_its_draft_and_the_report_is_the_whole_text(tmp_path, monkeypatch):
    fake_runner(monkeypatch)
    chunks = ["> **Verdict: Mostly luck**", " · Confidence: Medium\n\n", "A report."]
    run_streamed = pipeline.Runner.run_streamed  # the conftest stand-in, for the other agents
    monkeypatch.setattr(pipeline.Runner, "run_streamed", lambda agent, prompt, **kwargs: TextStream(chunks)
                        if agent.name == "Writer" else run_streamed(agent, prompt, **kwargs))
    events = []
    result = asyncio.run(pipeline.deep_research("Why?", [], AgentContexts(), events.append))
    drafts = [e for e in events if e["type"] in ("draft_start", "draft_delta")]
    assert drafts[0]["type"] == "draft_start"
    assert "".join(e.get("delta", "") for e in drafts) == "".join(chunks)
    assert result["markdown"].endswith("A report.")
