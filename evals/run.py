"""The test set: fixed questions with known answers, run at milestones to compare versions.

    uv run python -m evals.run score                 # free: scores the saved runs in logs/runs/
    uv run python -m evals.run score --commit 8d5010a
    uv run python -m evals.run run C1 C2 C3          # spends Vertex credits, after you confirm
    uv run python -m evals.run run why               # a group: why, clarifier or mixed

This isn't in tests/, so `uv run pytest` can't spend credits by accident.

Scoring reads the run records, so it checks what code can: the outcome, the verdict, whether the
required facts and words appear, and whether anything forbidden does. A fact can appear with the
wrong meaning, so read the reports too.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = Path(__file__).with_name("questions.json")
RUNS = ROOT / "logs" / "runs"


def load_questions() -> list[dict]:
    return json.loads(QUESTIONS.read_text(encoding="utf-8"))


def pick(questions: list[dict], names: list[str]) -> list[dict]:
    """Questions by id or group, in file order."""
    known = {q["id"] for q in questions} | {q["group"] for q in questions}
    if unknown := [n for n in names if n not in known]:
        raise SystemExit(f"Unknown id or group: {', '.join(unknown)}. Choose from: {', '.join(sorted(known))}")
    return [q for q in questions if q["id"] in names or q["group"] in names]


# --- Scoring: no model calls ---


def has_value(text: str, value: str) -> bool:
    """A number as a whole number, not part of a longer one: "3.82" isn't in "13.82". ".281" also
    matches "0.281". Anything else, like a name, is a plain search."""
    if re.fullmatch(r"\d*\.?\d+", value):
        body = re.escape(value.lstrip("0") if value.startswith("0.") else value)
        prefix = r"(?<![\d.])0?" if value.startswith(".") else r"(?<![\d.])"
        return re.search(prefix + body + r"(?!\d)", text) is not None
    return value.lower() in text.lower()


def record_text(record: dict) -> str:
    """What the user was shown: the report, or the clarification and its options."""
    result = record.get("result") or {}
    parts = [result.get("markdown", ""), result.get("message", "")]
    for q in result.get("questions", []):
        parts += [q.get("question", ""), *q.get("options", [])]
    return "\n".join(p for p in parts if p)


def verdict_of(text: str) -> tuple[str, str]:
    verdict = re.search(r"Verdict:\s*([^*·\n]+)", text)
    confidence = re.search(r"Confidence:\s*(\w+)", text)
    return (verdict.group(1).strip() if verdict else "", confidence.group(1) if confidence else "")


def overlap_of(text: str) -> str:
    match = re.search(r"Injury overlap:\s*(\w+)", text)
    return match.group(1) if match else ""


def score(question: dict, record: dict) -> dict:
    text = record_text(record)
    verdict, confidence = verdict_of(text)
    facts = question.get("facts", [])
    found = [f for f in facts if has_value(text, f)]
    allowed = question.get("verdicts")
    checks = {
        "outcome": record.get("status") == question["outcome"],
        "verdict": verdict in allowed if allowed else None,
        "confidence": confidence not in question["confidence_not"] if "confidence_not" in question else None,
        "overlap": overlap_of(text) == question["overlap"] if "overlap" in question else None,
        "must_have": all(w.lower() in text.lower() for w in question["must_have"]) if "must_have" in question else None,
        "must_not": not any(re.search(p, text) for p in question["must_not"]) if "must_not" in question else None,
    }
    result = record.get("result") or {}
    check = result.get("verification") or {}
    return {"id": question["id"], "status": record.get("status"), "verdict": verdict, "confidence": confidence,
            "checks": checks, "passed": all(v is not False for v in checks.values()),
            "facts_found": len(found), "facts_total": len(facts),
            "facts_missing": [f for f in facts if f not in found],
            "failed_queries": len(record.get("failed_queries", [])),
            "queries": len(record.get("evidence", {})),
            "citations": f"{check.get('numbers_matched')}/{check.get('numbers_checked')}" if check else "-",
            "seconds": record.get("seconds"), "tokens_in": (record.get("tokens") or {}).get("input", 0),
            "tokens_out": (record.get("tokens") or {}).get("output", 0),
            "commit": (record.get("settings") or {}).get("git_commit", "")}


def load_records(directory: Path = RUNS) -> list[dict]:
    """Saved runs, oldest first. File names start with the time."""
    records = []
    for path in sorted(directory.glob("*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except ValueError:
            print(f"skipped unreadable record: {path.name}", file=sys.stderr)
    return records


def latest_for(question: dict, records: list[dict], commit: str = "") -> dict | None:
    matches = [r for r in records if r.get("question") == question["question"]
               and (r.get("settings") or {}).get("git_commit", "").startswith(commit)]
    return matches[-1] if matches else None


def show(rows: list[dict]) -> None:
    head = f"{'id':4}{'pass':6}{'verdict':24}{'conf':8}{'facts':7}{'failed':8}{'queries':9}{'cites':8}{'secs':7}{'in':>9}{'out':>8}  commit"
    print(head)
    for s in rows:
        print(f"{s['id']:4}{('yes' if s['passed'] else 'NO'):6}{(s['verdict'] or s['status'] or '')[:22]:24}{s['confidence']:8}"
              f"{s['facts_found']}/{s['facts_total']:<5}{s['failed_queries']:<8}{s['queries']:<9}{s['citations']:8}"
              f"{s['seconds'] or 0:<7.0f}{s['tokens_in']:>9,}{s['tokens_out']:>8,}  {s['commit']}")
    for s in rows:
        failed = [k for k, v in s["checks"].items() if v is False]
        if failed or s["facts_missing"]:
            print(f"  {s['id']}: failed {failed or 'nothing'}; facts not in the report: {s['facts_missing'] or 'none'}")
    if rows:
        print(f"\n{sum(s['passed'] for s in rows)}/{len(rows)} passed | facts {sum(s['facts_found'] for s in rows)}"
              f"/{sum(s['facts_total'] for s in rows)} | failed queries {sum(s['failed_queries'] for s in rows)} of "
              f"{sum(s['failed_queries'] + s['queries'] for s in rows)} | tokens in {sum(s['tokens_in'] for s in rows):,}, "
              f"out {sum(s['tokens_out'] for s in rows):,}")


def cmd_score(args) -> None:
    questions = load_questions()
    chosen = pick(questions, args.names) if args.names else questions
    records = load_records()
    rows, missing = [], []
    for q in chosen:
        record = latest_for(q, records, args.commit)
        if record:
            rows.append(score(q, record))
        else:
            missing.append(q["id"])
    show(rows)
    if missing:
        print(f"No saved run for: {', '.join(missing)}")


# --- Running: this spends credits ---


async def research(questions: list[dict]) -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")  # before the pipeline import, which reads env vars

    from agents import set_tracing_disabled

    from agent.pipeline import deep_research
    from context_panel import AgentContexts

    set_tracing_disabled(True)
    for q in questions:  # one at a time, so runs don't compete for rate limits
        print(f"--- {q['id']}: {q['question']}", flush=True)
        await deep_research(q["question"], [], AgentContexts(), lambda event: None, run_id=uuid.uuid4().hex)


def cmd_run(args) -> None:
    questions = pick(load_questions(), args.names)
    full = [q["id"] for q in questions if q["outcome"] == "done"]
    print(f"{len(questions)} question(s): {', '.join(q['id'] for q in questions)}. {len(full)} should be full research "
          "runs, about 170,000 to 400,000 input tokens each.")
    if not args.yes and input("This spends Vertex credits. Type yes to continue: ").strip().lower() != "yes":
        raise SystemExit("Stopped. Nothing was run.")
    asyncio.run(research(questions))
    print()
    records = load_records()
    show([score(q, r) for q in questions if (r := latest_for(q, records))])


def main() -> None:
    parser = argparse.ArgumentParser(description="Run or score the test set.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run questions; spends credits")
    run.add_argument("names", nargs="+", help="question ids or groups")
    run.add_argument("--yes", action="store_true", help="skip the confirmation")
    run.set_defaults(func=cmd_run)
    scorer = commands.add_parser("score", help="score saved runs; free")
    scorer.add_argument("names", nargs="*", help="question ids or groups; all by default")
    scorer.add_argument("--commit", default="", help="only runs from this commit")
    scorer.set_defaults(func=cmd_score)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
