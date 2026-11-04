"""Every run leaves a record in RUNS_DIR, however it ends. No model calls: the Clarifier is stubbed."""

import asyncio
import datetime as dt
import json

import pytest

from agent import pipeline
from agent.runs import prompt_hash, save_run
from context_panel import AgentContexts


def run_files(path):
    return [json.loads(f.read_text()) for f in sorted(path.glob("*.json"))]


def research(monkeypatch, name, stub):
    monkeypatch.setattr(pipeline, name, stub)
    return asyncio.run(pipeline.deep_research("Why?", [], AgentContexts(), lambda event: None, run_id="abcd1234-rest"))


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    return tmp_path


def test_a_record_is_written_and_values_json_cant_hold_become_text(saved):
    path = save_run({"run_id": "abcd1234-rest", "when": dt.date(2026, 5, 1)})
    assert path.name.endswith("-abcd1234.json")
    assert json.loads(path.read_text())["when"] == "2026-05-01"


def test_an_empty_runs_dir_turns_saving_off(monkeypatch):
    monkeypatch.setenv("RUNS_DIR", "")
    assert save_run({"run_id": "x"}) is None


def test_off_by_default_on_cloud_run(monkeypatch):
    monkeypatch.delenv("RUNS_DIR", raising=False)
    monkeypatch.setenv("K_SERVICE", "pitch-lab")
    assert save_run({"run_id": "x"}) is None


def test_a_save_that_fails_doesnt_raise(tmp_path, monkeypatch):
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    monkeypatch.setenv("RUNS_DIR", str(blocker / "runs"))
    assert save_run({"run_id": "x"}) is None


def test_prompt_hash_changes_with_the_prompts():
    assert prompt_hash("a", "b") == prompt_hash("a", "b") != prompt_hash("a", "c")


def test_a_clarification_is_saved(saved, monkeypatch):
    async def clarify(question, answers, rounds, run, hooks):
        return pipeline.Clarification(status="out_of_scope", question=question, message="No Stuff+ here.")

    assert research(monkeypatch, "clarify", clarify)["type"] == "clarify"
    [record] = run_files(saved)
    assert record["status"] == "clarify" and record["run_id"] == "abcd1234-rest"
    assert record["result"]["message"] == "No Stuff+ here."
    assert record["settings"]["prompt_hash"] and record["settings"]["researcher_model"]


def test_a_failed_run_is_saved_with_the_work_it_finished(saved, monkeypatch):
    async def research_run(question, answers, rounds, force, run, hooks):
        run.findings.append({"id": 1, "text": "FINDINGS:\n- a finding [Q1]"})
        raise RuntimeError("quota")

    with pytest.raises(RuntimeError):
        research(monkeypatch, "research_run", research_run)
    [record] = run_files(saved)
    assert record["status"] == "error" and "quota" in record["error"]
    assert record["findings"][0]["id"] == 1


def test_a_stopped_run_is_saved(saved, monkeypatch):
    async def research_run(question, answers, rounds, force, run, hooks):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        research(monkeypatch, "research_run", research_run)
    assert run_files(saved)[0]["status"] == "stopped"
