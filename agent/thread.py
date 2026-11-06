"""A conversation: its earlier questions, their reports, and the evidence those reports cite.

A follow-up run starts its ledger from this evidence, so query ids keep counting up (Q1-Q22 from the
first question, Q23 onward from the next), earlier citations stay valid, and verify.py can check a
number the new report takes from an earlier one. Researchers don't see any of it: they stay in fresh
contexts, and the Lead tells them what they need in their task.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .evidence import Evidence, Ledger

# Earlier reports an agent reads in full. Older questions stay as one line each, so a long conversation
# doesn't grow every model call. Their evidence stays citable either way.
MAX_REPORTS = 3


@dataclass
class Thread:
    turns: list[dict] = field(default_factory=list)  # {"question", "report", "pitchers": [{"mlbam_id", "name"}]}
    evidence: dict[str, Evidence | None] = field(default_factory=dict)

    def ledger(self) -> Ledger:
        """A copy for one run, so a run that fails or is stopped leaves the conversation as it was.
        It keeps reserved ids too: Ledger numbers new queries by how many ids it holds."""
        return Ledger(items=dict(self.evidence))

    def queries(self) -> int:
        return sum(1 for ev in self.evidence.values() if ev is not None)

    def remember(self, question: str, report: str, pitchers: dict[int, str], ledger: Ledger) -> None:
        self.turns.append({"question": question, "report": report,
                           "pitchers": [{"mlbam_id": k, "name": v} for k, v in pitchers.items()]})
        self.evidence = dict(ledger.items)

    def questions(self) -> str:
        return "\n".join(f"- {t['question']}" for t in self.turns)

    def pitchers(self) -> str:
        seen = {p["mlbam_id"]: p["name"] for t in self.turns for p in t["pitchers"]}
        return ", ".join(f"{name} (id {pid})" for pid, name in seen.items()) or "none named"

    def context(self) -> str:
        """The conversation so far, as the Clarifier, Lead and Writer read it."""
        older, recent = self.turns[:-MAX_REPORTS], self.turns[-MAX_REPORTS:]
        parts = [f"Pitchers so far: {self.pitchers()}"]
        if older:
            parts.append("Earlier questions:\n" + "\n".join(f"- {t['question']}" for t in older))
        parts += [f"## Earlier question: {t['question']}\n\n{t['report']}" for t in recent]
        return "\n\n".join(parts)
