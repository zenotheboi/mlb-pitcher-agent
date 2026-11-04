"""Tests never spend credits: any real model call is blocked, and fails the test that made it."""

import litellm
import pytest
from agents import set_tracing_disabled

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
