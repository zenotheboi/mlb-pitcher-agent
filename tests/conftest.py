"""Tests never spend credits: any real model call is blocked, and fails the test that made it."""

import litellm
import pytest
from agents import Runner, set_tracing_disabled

set_tracing_disabled(True)


@pytest.fixture(autouse=True)
def model_calls(monkeypatch, tmp_path):
    calls = []

    async def blocked(*args, **kwargs):
        calls.append(kwargs.get("model"))
        raise RuntimeError("Tests must not call a real model.")

    monkeypatch.setattr(litellm, "acompletion", blocked)
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))  # keep test runs out of logs/runs
    yield calls
    assert not calls, f"A test called a real model: {calls}"


class _Streamed:
    """Runner.run_streamed's result, from whatever Runner.run is at the time: tests fake Runner.run,
    and the Writer streams. No text deltas, just the final output."""

    def __init__(self, run):
        self._run = run
        self.final_output = None

    async def stream_events(self):
        self.final_output = (await self._run).final_output
        return
        yield


@pytest.fixture(autouse=True)
def streamed_runs_use_run(monkeypatch):
    monkeypatch.setattr(Runner, "run_streamed", lambda agent, prompt, **kwargs: _Streamed(Runner.run(agent, prompt, **kwargs)))
