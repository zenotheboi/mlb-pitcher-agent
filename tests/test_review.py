"""The review loop: Critic feedback, the retry limit, keeping the best version, and showing what's left."""

import asyncio
from types import SimpleNamespace

import pytest

from agent import pipeline
from agent.evidence import Evidence, Ledger
from agent.pipeline import CriticReview
from agent.review import CriticProblem, confirmed, present, unresolved
from agent.tools import RunState

GOOD = "> **Verdict: Mostly luck** · Confidence: High · Injury overlap: No\n\nHis ERA rose in 2026."
BAD = GOOD + " The injury caused it."
PROBLEM = CriticProblem(quote="The injury caused it.", issue="No finding shows a cause.")


def ledger():
    led = Ledger()
    led.items["Q1"] = Evidence(id="Q1", by="t", purpose="p", sql="select 1", columns=["k_pct"], rows=[[0.25]],
                               truncated=False, tables=[], sources=[], elapsed_ms=1)
    return led


def run_review(monkeypatch, draft, critic_says, writer_replies=(), **limits):
    """Runs the loop with scripted agents. critic_says(report) gives the Critic's problems for a report.
    Returns the result with the review log added, and the calls made."""
    seen = []
    replies = iter(writer_replies)

    async def run(agent, prompt, **kwargs):
        seen.append((agent.name, prompt))
        if agent.name.startswith("Critic"):
            report = prompt.split("# The report\n")[1].split("\n\n# The queries it cites")[0]
            return SimpleNamespace(final_output=CriticReview(problems=critic_says(report)))
        return SimpleNamespace(final_output=next(replies))

    monkeypatch.setattr(pipeline.Runner, "run", run)
    for name, value in limits.items():
        monkeypatch.setattr(pipeline, name, value)
    state = RunState(emit=lambda event: None, ledger=ledger())
    state.started = pipeline.time.time()
    c = pipeline.Clarification(status="clear", question="Why?")
    result = asyncio.run(pipeline.review_report(state, None, c, draft, "Writer input"))
    result["log"] = state.review_log
    return result, seen


def flags_the_claim(report):
    return [PROBLEM] if PROBLEM.quote in report else []


def edit_removing_the_claim():
    return '[{"find": " The injury caused it.", "replace": ""}]'


def test_a_clean_report_goes_straight_through(monkeypatch):
    result, seen = run_review(monkeypatch, GOOD, lambda report: [])
    assert result["report"] == GOOD and result["revisions"] == 0 and [n for n, _ in seen] == ["Critic"]


def test_the_critics_feedback_reaches_the_writer_and_an_edit_fixes_it(monkeypatch):
    result, seen = run_review(monkeypatch, BAD, flags_the_claim, [edit_removing_the_claim()])
    assert result["report"] == GOOD and result["revisions"] == 1 and result["unresolved"] == []
    assert "No finding shows a cause." in dict(seen)["Writer (revise)"]


def test_every_review_and_revision_is_logged(monkeypatch):
    result, _ = run_review(monkeypatch, BAD, flags_the_claim, [edit_removing_the_claim()])
    first, edit, recheck = result["log"]
    assert first["reviewer"] == "Critic" and first["returned"][0]["quote"] == PROBLEM.quote and first["kept"] == 1
    assert edit["step"] == "edits" and edit["changed"] and "No finding shows a cause." in edit["feedback"]
    assert recheck["reviewer"] == "Critic (re-check)" and recheck["returned"] == []


def test_a_second_try_is_a_full_rewrite(monkeypatch):
    result, seen = run_review(monkeypatch, BAD, flags_the_claim, ["[]", GOOD])
    assert [n for n, _ in seen] == ["Critic", "Writer (revise)", "Critic (re-check)", "Writer (rewrite)", "Critic (re-check)"]
    assert result["report"] == GOOD and result["revisions"] == 2


def test_the_first_review_is_thorough_and_re_checks_are_quick():
    effort = lambda agent: agent.model_settings.extra_args.get("reasoning_effort")
    assert effort(pipeline.critic) is None and effort(pipeline.rechecker) == "low"
    assert pipeline.rechecker.output_type is pipeline.critic.output_type


def test_a_wrong_verdict_goes_straight_to_a_rewrite(monkeypatch):
    wrong = CriticProblem(quote="Verdict: Mostly luck", issue="The z scores don't support it.", whole_report=True)
    fixed = GOOD.replace("Mostly luck", "Not enough evidence")
    result, seen = run_review(monkeypatch, GOOD, lambda r: [wrong] if "Mostly luck" in r else [], [fixed])
    assert "Writer (rewrite)" in dict(seen) and "Writer (revise)" not in dict(seen) and result["report"] == fixed


def test_the_retry_limit_holds_and_the_risk_is_shown(monkeypatch):
    result, seen = run_review(monkeypatch, BAD, flags_the_claim, ["[]", BAD])
    assert sum(n.startswith("Critic") for n, _ in seen) == 3 and result["revisions"] == 2
    assert "Unresolved issues (1)" in result["report"] and "couldn't be confirmed" in result["report"]
    assert "No finding shows a cause." in result["report"]


def test_unresolved_problems_cap_confidence_at_medium(monkeypatch):
    result, _ = run_review(monkeypatch, BAD, flags_the_claim, ["[]", BAD])
    assert "Confidence: Medium" in result["report"] and "Confidence: High" not in result["report"] and result["capped"]


def test_the_best_version_is_kept_when_a_rewrite_is_worse(monkeypatch):
    worse = BAD + " His arm fell off. He retired."
    extra = CriticProblem(quote="His arm fell off.", issue="Not in the evidence.")
    says = lambda report: [p for p in (PROBLEM, extra) if p.quote in report]
    result, _ = run_review(monkeypatch, BAD, says, ["[]", worse])
    assert "His arm fell off" not in result["report"].split("couldn't be confirmed")[0] and len(result["unresolved"]) == 1


def test_the_limit_can_be_changed(monkeypatch):
    result, seen = run_review(monkeypatch, BAD, flags_the_claim, MAX_REVISIONS=0)
    assert result["revisions"] == 0 and [n for n, _ in seen] == ["Critic"] and "Unresolved issues" in result["report"]


def test_no_new_revision_past_the_time_budget(monkeypatch):
    result, _ = run_review(monkeypatch, BAD, flags_the_claim, REVIEW_UNTIL=-1)
    assert result["revisions"] == 0 and "Unresolved issues" in result["report"]


def test_a_failed_critic_doesnt_stop_the_report(monkeypatch):
    async def run(agent, prompt, **kwargs):
        raise RuntimeError("the critic broke")

    monkeypatch.setattr(pipeline.Runner, "run", run)
    state = RunState(emit=lambda event: None, ledger=ledger())
    state.started = pipeline.time.time()
    result = asyncio.run(pipeline.review_report(state, None, pipeline.Clarification(question="Why?"), GOOD, "x"))
    assert result["report"] == GOOD and state.fallbacks == ["Critic"]


def test_a_rewrite_with_no_verdict_is_rejected(monkeypatch):
    result, _ = run_review(monkeypatch, BAD, flags_the_claim, ["[]", "Sorry, I can't."])
    assert "Verdict: Mostly luck" in result["report"]


NOTE = CriticProblem(quote="His ERA rose in 2026.", issue="It could say by how much.", kind="wording")


def test_a_wording_note_reaches_the_writer_but_never_the_reader(monkeypatch):
    says = lambda report: [NOTE]
    result, seen = run_review(monkeypatch, GOOD, says, ["[]"])
    assert "It could say by how much." in dict(seen)["Writer (revise)"]
    assert result["report"] == GOOD and result["unresolved"] == [] and not result["capped"]


def test_a_wording_note_gets_one_edit_and_no_rewrite(monkeypatch):
    result, seen = run_review(monkeypatch, GOOD, lambda report: [NOTE], ["[]"])
    assert result["revisions"] == 1 and "Writer (rewrite)" not in dict(seen)


def test_only_claims_cap_confidence(monkeypatch):
    says = lambda report: [NOTE, PROBLEM]
    result, _ = run_review(monkeypatch, BAD, says, ["[]", BAD])
    assert result["capped"] and len(result["unresolved"]) == 1 and "could say by how much" not in result["report"]


def test_invented_problems_are_dropped():
    real = CriticProblem(quote="his  **ERA** rose", issue="x", query="Q1")
    made_up = CriticProblem(quote="This sentence isn't there.", issue="x")
    bad_query = CriticProblem(quote="His ERA rose", issue="x", query="Q99")
    assert confirmed([real, made_up, bad_query], GOOD, ledger()) == [real]


def test_the_risks_go_after_the_verdict_and_stay_in_the_markdown():
    report, capped = present(GOOD, ["A claim."])
    verdict, risks, body = report.split("\n\n", 2)
    assert verdict.startswith("> **Verdict") and "Unresolved issues (1)" in risks and capped
    assert "His ERA rose" in report.split("- A claim.")[1]


def test_nothing_changes_when_nothing_is_unresolved():
    assert present(GOOD, []) == (GOOD, False)


def test_code_check_failures_count_as_unresolved_too():
    check = {"unmatched": [{"number": "5.1", "sentence": "It was 5.1.", "cited": ["Q1"]}], "uncited": [], "unknown_ids": ["Q9"]}
    items = unresolved(check, [])
    assert len(items) == 2 and "5.1" in items[0] and "Q9" in items[1]
