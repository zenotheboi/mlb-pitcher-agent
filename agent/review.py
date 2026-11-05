"""What the Critic reports, and what happens to problems nobody could fix.

The Critic judges meaning, which the citation check in verify.py can't. It's a model too, so its
findings are checked against the report and the ledger, and what stays unresolved is shown as
something that couldn't be confirmed, not as a known error.
"""

from __future__ import annotations

import re

from typing import Literal

from pydantic import BaseModel, Field

from .evidence import Ledger, as_markdown
from .verify import CITE, QID

MAX_SHOWN = 8  # unresolved issues listed in a report


class CriticProblem(BaseModel):
    quote: str  # the exact text it objects to
    issue: str  # what's wrong, in one sentence
    query: str = ""  # the query that shows it, like Q7, if one does
    kind: Literal["claim", "wording"] = "claim"  # a claim may be wrong; wording is right but could read better
    whole_report: bool = False  # the verdict or confidence is wrong, so edits won't fix it


class CriticReview(BaseModel):
    problems: list[CriticProblem] = Field(default_factory=list)


def _plain(text: str) -> str:
    return " ".join(text.replace("*", "").split()).lower()


def cited_rows(report: str, ledger: Ledger, limit: int = 30) -> str:
    """The query results a report cites, for whoever is checking or fixing it."""
    ids = sorted({q for group in CITE.findall(report) for q in QID.findall(group)}, key=lambda q: int(q[1:]))
    return "\n\n".join(as_markdown(ledger.get(q)) for q in ids[:limit] if ledger.get(q))


def critic_input(question: str, unanswerable: list[str], chance_check: str, report: str, ledger: Ledger) -> str:
    parts = [f"Question: {question}", f"# The report\n{report}", f"# The queries it cites\n{cited_rows(report, ledger)}"]
    if chance_check:
        parts.append(f"# Chance check for the periods in the question\n{chance_check}")
    if unanswerable:
        parts.append(f"# What the data can't answer, which the report must say plainly\n{'; '.join(unanswerable)}")
    return "\n\n".join(parts)


def confirmed(problems: list[CriticProblem], report: str, ledger: Ledger) -> list[CriticProblem]:
    """Keep problems about text that's really in the report, resting on queries that really ran."""
    text = _plain(report)
    return [p for p in problems if _plain(p.quote) in text and (not p.query or ledger.get(p.query))]


def claims(problems: list[CriticProblem]) -> list[CriticProblem]:
    """The problems that matter to a reader. Wording notes are only for the Writer."""
    return [p for p in problems if p.kind == "claim"]


def count(check: dict, problems: list[CriticProblem]) -> int:
    return check["issues"] + len(claims(problems))


def feedback(check: dict, problems: list[CriticProblem], code_issues: str) -> str:
    lines = [code_issues] if code_issues else []
    lines += [f"- {p.quote!r}: {p.issue}" + (f" (see {p.query})" if p.query else "") for p in problems]
    return "\n".join(lines)


def unresolved(check: dict, problems: list[CriticProblem]) -> list[str]:
    items = [f"\"{p.quote[:160]}\": {p.issue}" for p in claims(problems)]
    items += [f"{u['number']} couldn't be matched to the query it cites: \"{u['sentence'][:120]}\"" for u in check["unmatched"]]
    items += [f"No source for {', '.join(u['numbers'])}: \"{u['sentence'][:120]}\"" for u in check["uncited"]]
    items += [f"[{q}] isn't a query that ran." for q in check["unknown_ids"]]
    return items


def present(report: str, items: list[str]) -> tuple[str, bool]:
    """Show what couldn't be confirmed in the report itself, so it survives being copied, and cap the
    confidence at Medium. Returns the report and whether the confidence was lowered."""
    if not items:
        return report, False
    report, capped = re.subn(r"(Confidence:\s*\**\s*)High\b", r"\1Medium", report, count=1)
    block = [f"**Unresolved issues ({len(items)}):** these couldn't be confirmed after review.", ""]
    block += [f"- {item}" for item in items[:MAX_SHOWN]]
    lines = report.split("\n")
    end = next((i for i, line in enumerate(lines) if not line.startswith(">")), len(lines))
    return "\n".join(lines[:end] + ["", *block, ""] + lines[end:]), bool(capped)
