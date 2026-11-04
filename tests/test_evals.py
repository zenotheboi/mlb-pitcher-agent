"""The test set's scoring. No model calls, and nothing here runs a question."""

import json

import pytest

from evals import run as evals


def record(markdown="", status="done", **fields):
    return {"status": status, "question": "Q?", "seconds": 90, "tokens": {"input": 400000, "output": 20000},
            "settings": {"git_commit": "abc1234"}, "evidence": {"Q1": {}}, "failed_queries": [],
            "result": {"markdown": markdown, "verification": {"numbers_matched": 5, "numbers_checked": 5}}, **fields}


QUESTION = {"id": "T1", "group": "why", "question": "Q?", "outcome": "done", "verdicts": ["Mostly luck"],
            "facts": ["22.5", ".288"]}
REPORT = "> **Verdict: Mostly luck** · Confidence: Medium\n\nK% fell to 22.5% and xwOBA was .288 [Q1]."


def test_every_question_is_well_formed():
    questions = evals.load_questions()
    assert len({q["id"] for q in questions}) == len(questions) == 10
    for q in questions:
        assert q["outcome"] in ("done", "clarify") and q["group"] in ("why", "clarifier", "mixed") and q["question"]


def test_groups_and_ids_can_be_picked():
    questions = evals.load_questions()
    assert [q["id"] for q in evals.pick(questions, ["clarifier"])] == ["C1", "C2", "C3"]
    assert [q["id"] for q in evals.pick(questions, ["W2", "M1"])] == ["W2", "M1"]


def test_an_unknown_id_stops_before_anything_runs():
    with pytest.raises(SystemExit):
        evals.pick(evals.load_questions(), ["W9"])


@pytest.mark.parametrize("text, value, expected", [
    ("fell to 33.2%", "33.2", True), ("was 133.2 pitches", "33.2", False), ("was 33.25", "33.2", False),
    ("xwOBA of .281", ".281", True), ("xwOBA of 0.281", ".281", True), ("xwOBA of 1.281", ".281", False),
    ("Bailey Ober lost velocity", "Ober", True),
])
def test_facts_match_whole_numbers(text, value, expected):
    assert evals.has_value(text, value) is expected


def test_a_good_report_passes():
    s = evals.score(QUESTION, record(REPORT))
    assert s["passed"] and s["verdict"] == "Mostly luck" and s["confidence"] == "Medium"
    assert s["facts_found"] == 2 and s["citations"] == "5/5"


def test_a_wrong_verdict_fails_and_missing_facts_are_named():
    s = evals.score(QUESTION, record("> **Verdict: Real skill decline** · Confidence: High\n\nK% was 22.5%."))
    assert not s["passed"] and s["checks"]["verdict"] is False and s["facts_missing"] == [".288"]


def test_a_stopped_or_failed_run_fails():
    assert not evals.score(QUESTION, record(status="error"))["passed"]


def m1_rule():
    return next(q for q in evals.load_questions() if q["id"] == "M1")["must_not"]


def test_confidence_limit():
    q = {**QUESTION, "confidence_not": ["High"]}
    assert evals.score(q, record("> **Verdict: Mostly luck** · Confidence: High"))["checks"]["confidence"] is False


@pytest.mark.parametrize("sentence", [
    "His xFIP was about 3.12.", "His xFIP rose to 3.30.", "xFIP: 3.12.", "An xFIP of 3.12 fits.", "His xFIP (3.12) rose.",
    "His xFIP also rose.", "xFIP fell slightly.",
])
def test_stating_an_xfip_value_or_direction_is_forbidden(sentence):
    q = {**QUESTION, "must_not": m1_rule()}
    assert evals.score(q, record(REPORT + " " + sentence))["checks"]["must_not"] is False


@pytest.mark.parametrize("sentence", [
    "xFIP isn't in our data, but his FIP was 3.33.", "We can't say whether xFIP rose.", "xFIP isn't available.",
    "We don't have xFIP. His FIP rose to 3.33.",
])
def test_saying_xfip_is_unavailable_is_allowed(sentence):
    q = {**QUESTION, "must_have": ["xFIP"], "must_not": m1_rule()}
    s = evals.score(q, record(REPORT + " " + sentence))
    assert s["checks"]["must_have"] is True and s["checks"]["must_not"] is True


def test_injury_overlap_is_scored_separately_from_the_verdict():
    q = {**QUESTION, "overlap": "Yes"}
    line = "> **Verdict: Mostly luck** · Confidence: Medium · Injury overlap: {}"
    assert evals.score(q, record(line.format("Yes")))["checks"]["overlap"] is True
    no = evals.score(q, record(line.format("No")))
    assert no["checks"]["overlap"] is False and no["verdict"] == "Mostly luck" and no["confidence"] == "Medium"


def test_a_clarification_is_scored_from_its_message_and_options():
    q = {"id": "C1", "group": "clarifier", "question": "Q?", "outcome": "clarify", "must_have": ["Cade Smith"]}
    rec = record(status="clarify")
    rec["result"] = {"message": "Which Smith?", "questions": [{"question": "Who?", "options": ["Cade Smith (RHP)"]}]}
    assert evals.score(q, rec)["passed"]


def test_the_latest_run_is_used_and_can_be_limited_to_a_commit(tmp_path):
    for name, verdict, commit in [("1.json", "old", "aaa1111"), ("2.json", "new", "bbb2222")]:
        (tmp_path / name).write_text(json.dumps({"question": "Q?", "verdict": verdict, "settings": {"git_commit": commit}}))
    (tmp_path / "3.json").write_text("not json")
    records = evals.load_records(tmp_path)
    assert evals.latest_for(QUESTION, records)["verdict"] == "new"
    assert evals.latest_for(QUESTION, records, "aaa")["verdict"] == "old"
    assert evals.latest_for(QUESTION, records, "zzz") is None


def test_running_needs_a_typed_yes(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    monkeypatch.setattr(evals, "research", lambda questions: pytest.fail("it ran without confirmation"))
    args = type("Args", (), {"names": ["W1"], "yes": False})
    with pytest.raises(SystemExit):
        evals.cmd_run(args)
