"""Run one research question in the terminal, printing the event stream.

    uv run python -m agent.cli "Why did Paul Skenes' strikeout rate fall in 2026?"
"""

import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from agents import set_tracing_disabled  # noqa: E402

from context_panel import AgentContexts  # noqa: E402

from .pipeline import deep_research  # noqa: E402

set_tracing_disabled(True)


def show(event: dict) -> None:
    kind = event["type"]
    if kind in ("usage", "context"):
        return
    if kind == "activity":
        print(f"  · {event['agent']}: {event['text']}")
    elif kind == "query":
        print(f"  [{event['id']}] {event['agent']}: {event['purpose']} ({len(event['rows'])} rows)")
    elif kind == "researcher" and event["status"] != "running":
        print(f"\n--- {event['name']} ({event['focus']}, round {event['wave']}) {event['status']} in {event['seconds']}s\n{event['findings']}\n")
    else:
        print(json.dumps(event, default=str)[:400])


async def main(question: str, force: bool) -> None:
    result = await deep_research(question, [], AgentContexts(), show, force=force)
    if result["type"] == "clarify":
        print("\nCLARIFY:", json.dumps(result, indent=2))
        return
    print("\n" + "=" * 80 + "\n" + result["markdown"] + "\n" + "=" * 80)
    v = result["verification"]
    print(f"verification: {v['numbers_matched']}/{v['numbers_checked']} numbers matched, issues {v['issues']} "
          f"(draft had {v['draft_issues']}), unknown ids {v['unknown_ids']}")
    for u in v["unmatched"][:10]:
        print("  unmatched:", u["number"], "|", u["sentence"][:160])
    for u in v["uncited"][:10]:
        print("  uncited:", u["numbers"], "|", u["sentence"][:160])
    print("stats:", result["stats"])


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--force"]
    asyncio.run(main(" ".join(args), "--force" in sys.argv))
