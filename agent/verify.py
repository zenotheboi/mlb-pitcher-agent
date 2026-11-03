"""Check a report's citations against the evidence ledger, in code.

For every sentence of the report:
  1. Every [Q#] it cites must be a query that actually ran.
  2. Every number it states must appear in the rows of a query it cites: as is, as a percentage of
     a 0-1 rate (0.3307 -> 33.1%), or as the difference between two values the cited queries
     returned (a change: 98.8 - 96.8 -> 2.0 mph), at the precision the sentence uses.
  3. A sentence that states a number but cites nothing is flagged.

The pipeline sends the issues back to the Writer once to fix, then shows the final check on the page.
"""

from __future__ import annotations

import itertools
import re

from .evidence import Ledger

CITE = re.compile(r"\[(Q\d+(?:\s*[,;]\s*Q\d+)*)\]")
QID = re.compile(r"Q\d+")
# Strip things that contain digits but aren't claims: dates, links, ids, "60-day", "4-seam", "Q7"
NOISE = [
    re.compile(r"\]\([^)]*\)"),  # markdown link targets
    re.compile(r"https?://\S+"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\b", re.I),
    re.compile(r"\b\d+-(?:day|seam|pitch|game|man|inning|year|month|week|start)s?\b", re.I),
    re.compile(r"\bQ\d+\b"),
    re.compile(r"\b\d{6}\b"),  # MLBAM ids
    re.compile(r"\b(?:19|20)\d{2}(?:[-–]\d{2,4})?\b"),  # years, 2024-25
    re.compile(r"\b\d+/\d+\b"),  # 1/3 innings
]
NUMBER = re.compile(r"(?<![\w.])[-−+]?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?|\.\d+)(\s*%| percentage points?| pp\b)?")


def _sentences(markdown: str) -> list[str]:
    out = []
    for line in markdown.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or set(line) <= set("-|: "):
            continue
        # Split at sentence ends, but keep a citation that follows the period with its sentence
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z*(“\"])", line)
        out.extend(p for p in parts if p.strip())
    return out


def _numbers(sentence: str) -> list[tuple[float, int, str]]:
    """(value, decimals, text) for each number that states a claim."""
    text = CITE.sub(" ", sentence)
    for pattern in NOISE:
        text = pattern.sub(" ", text)
    found = []
    for m in NUMBER.finditer(text):
        raw = m.group(1).replace(",", "")
        value = float(raw)
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        is_pct = bool(m.group(2))
        # Small whole numbers are mostly counts in prose ("two stints", "3 starts"): too noisy to check
        if decimals == 0 and not is_pct and value <= 10:
            continue
        found.append((value, decimals, m.group(0).strip()))
    return found


def _values(ids: list[str], ledger: Ledger) -> dict[str, list[float]]:
    """Numeric values of the cited queries, by column name (so 2025 vs 2026 rows of the same
    column, even from two queries, can be differenced)."""
    cols: dict[str, list[float]] = {}
    for qid in ids:
        ev = ledger.get(qid)
        if not ev:
            continue
        for row in ev.rows:
            for col, v in zip(ev.columns, row):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    cols.setdefault(col, []).append(float(v))
    return cols


def _candidates(cols: dict[str, list[float]], rows: list[list[float]]) -> set[float]:
    base = [v for vs in cols.values() for v in vs]
    cands = set()
    for v in base:
        cands.update((abs(v), abs(v) * 100))
    # Changes: the same column across rows (season to season), and two columns of one row (wOBA - xwOBA)
    for vs in cols.values():
        for a, b in itertools.combinations(vs[:80], 2):
            d = abs(a - b)
            cands.update((d, d * 100))
            if b:
                cands.add(abs(d / b) * 100)  # relative change, in percent
    for row in rows:
        for a, b in itertools.combinations(row[:30], 2):
            d = abs(a - b)
            cands.update((d, d * 100))
    return cands


def _row_values(ids: list[str], ledger: Ledger) -> list[list[float]]:
    out = []
    for qid in ids:
        ev = ledger.get(qid)
        if ev:
            for row in ev.rows:
                out.append([float(v) for v in row if isinstance(v, (int, float)) and not isinstance(v, bool)])
    return out


def _matches(value: float, decimals: int, cands: set[float]) -> bool:
    # Half a unit of the precision the sentence uses: "6%" accepts 6.23%, ".281" rejects .284
    tol = 0.5 * 10 ** (-decimals) + 1e-6
    return any(abs(c - abs(value)) <= tol for c in cands)


def verify(markdown: str, ledger: Ledger) -> dict:
    known = {k for k, v in ledger.items.items() if v is not None}
    cited_ids: set[str] = set()
    unknown: set[str] = set()
    unmatched, uncited = [], []
    checked = matched = 0
    cache: dict[tuple, set[float]] = {}

    for sentence in _sentences(markdown):
        ids = [q for group in CITE.findall(sentence) for q in QID.findall(group)]
        cited_ids.update(ids)
        unknown.update(q for q in ids if q not in known)
        nums = _numbers(sentence)
        if not nums:
            continue
        real = [q for q in ids if q in known]
        if not real:
            uncited.append({"sentence": sentence[:300], "numbers": [t for _, _, t in nums]})
            continue
        key = tuple(sorted(real))
        if key not in cache:
            cache[key] = _candidates(_values(real, ledger), _row_values(real, ledger))
        for value, decimals, text in nums:
            checked += 1
            if _matches(value, decimals, cache[key]):
                matched += 1
            else:
                unmatched.append({"sentence": sentence[:300], "number": text, "cited": real})

    issues = len(unknown) + len(unmatched) + len(uncited)
    return {
        "citations": sorted(cited_ids, key=lambda q: int(q[1:])),
        "unknown_ids": sorted(unknown),
        "numbers_checked": checked,
        "numbers_matched": matched,
        "unmatched": unmatched,
        "uncited": uncited,
        "issues": issues,
        "score": round(matched / checked, 3) if checked else None,
    }


def issues_for_writer(result: dict) -> str:
    lines = []
    for q in result["unknown_ids"]:
        lines.append(f"- [{q}] is not a query that ran. Cite only ids that appear in the findings.")
    for u in result["unmatched"]:
        lines.append(f"- \"{u['number']}\" in: {u['sentence']!r} does not match the rows of {', '.join(u['cited'])}. "
                     "Use the exact value (or its change) from a cited query, cite the query it comes from, or remove it.")
    for u in result["uncited"]:
        lines.append(f"- No citation for {', '.join(u['numbers'])} in: {u['sentence']!r}. Add the [Q#] it comes from, or remove the number.")
    return "\n".join(lines[:25])
