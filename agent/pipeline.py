"""Deep research over the MLB pitcher database.

    Clarifier  checks the question is specific enough; if not, asks the user (and stops).
    Lead       plans, fans out 2-4 Researchers in one turn (they run in parallel, each in a fresh
               context), reads their findings and GAPS, and sends follow-up rounds for gaps.
    Researcher one task, one focus: queries the database; every result carries a citation id.
    Writer     sees only the question, the Lead's brief and the findings; writes the cited report.
    verify     code checks every cited number against the query rows; the Writer fixes issues once.

Prompts ask for restraint; code enforces it: researcher, round and query caps live here and in tools.py.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import time
import uuid
from typing import Literal

import litellm
from agents import (Agent, GuardrailFunctionOutput, ModelSettings, RunContextWrapper, Runner, function_tool,
                    input_guardrail)
from agents.exceptions import InputGuardrailTripwireTriggered, MaxTurnsExceeded
from agents.extensions.models.litellm_model import LitellmModel
from pydantic import BaseModel, Field, ValidationError

from context_panel import AgentContexts, PanelHooks

from . import prompts
from .evidence import as_markdown
from .logs import current_run, setup as setup_logging
from .runs import git_commit, prompt_hash, save_run
from .tools import RESEARCH_TOOLS, RunState, Scope, chance_checks, find_pitcher, lookup_pitchers, pitcher_overview
from .verify import issues_for_writer, verify

log = setup_logging()
litellm.suppress_debug_info = True
for noisy in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "httpx"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# LiteLLM routes the calls to Gemini on Vertex AI; api_key is unused (Vertex uses gcloud credentials).
MODEL = LitellmModel(model=os.environ.get("LEAD_MODEL", "vertex_ai/gemini-3.5-flash"), api_key="unused")
# Researchers run many short tool loops: the cheaper, faster model is enough.
RESEARCHER_MODEL = LitellmModel(model=os.environ.get("RESEARCHER_MODEL", "vertex_ai/gemini-3.5-flash-lite"), api_key="unused")
# A stalled model call would hang the page: give up after 90 s, and retry twice.
SETTINGS = ModelSettings(extra_args={"vertex_location": os.environ.get("VERTEX_LOCATION", "global"),
                                     "timeout": 90, "num_retries": 2})
# The Writer works from finished findings: at default effort Gemini spent ~24k thinking tokens
# (~115 s) on a report it writes in ~25 s at low effort. Measured on a 7-researcher run.
WRITER_SETTINGS = ModelSettings(extra_args={**SETTINGS.extra_args, "timeout": 150,
                                            "reasoning_effort": os.environ.get("WRITER_EFFORT", "low")})

MAX_RESEARCHERS = int(os.environ.get("MAX_RESEARCHERS", 8))  # in all, across rounds
MAX_ROUNDS = int(os.environ.get("MAX_ROUNDS", 3))  # the first fan-out plus 2 follow-ups for gaps
# Model calls per researcher. 8 successful queries + a few failed ones + find_pitcher + the answer
# fit in 12; past it, wrap_up() still gets its findings out.
RESEARCHER_TURNS = 12
MAX_CLARIFY_ROUNDS = 2  # after this many rounds of questions, research the best reading
# Time budget (seconds since the run started). A follow-up round starts only before FOLLOWUP_UNTIL;
# past RESEARCH_UNTIL the Lead is told to wrap up. Keeps a run well inside a 5-minute limit.
FOLLOWUP_UNTIL = float(os.environ.get("FOLLOWUP_UNTIL", 150))
RESEARCH_UNTIL = float(os.environ.get("RESEARCH_UNTIL", 200))
Focus = Literal["stuff", "results_luck", "health_workload", "history_context", "league_scan"]


# --- Hooks: the context panel's, plus round counting and token usage ---


class ResearchHooks(PanelHooks):
    def __init__(self, contexts: AgentContexts, emit):
        super().__init__(contexts, emit)
        self.run: RunState | None = None
        self.tokens = {"input": 0, "output": 0, "calls": 0}
        self.tokens_by_agent: dict[str, dict] = {}  # all the Researchers together, for the run record

    def count(self, agent_name: str, usage) -> None:
        group = re.sub(r" \d+$", "", agent_name)
        for tally in (self.tokens, self.tokens_by_agent.setdefault(group, {"input": 0, "output": 0, "calls": 0})):
            tally["calls"] += 1
            if usage:
                tally["input"] += getattr(usage, "input_tokens", 0) or 0
                tally["output"] += getattr(usage, "output_tokens", 0) or 0

    async def on_llm_end(self, context, agent, response):
        await super().on_llm_end(context, agent, response)
        if agent.name == "Lead" and self.run:
            self.run.lead_turns += 1  # research calls from the same Lead reply form one round
        self.count(agent.name, getattr(response, "usage", None))
        self.emit({"type": "usage", **self.tokens})


# --- Agents ---

class TopicCheck(BaseModel):
    on_topic: bool = True
    message: str = ""


topic_checker = Agent(name="Topic check", instructions=prompts.TOPIC_CHECK, model=RESEARCHER_MODEL,
                      model_settings=SETTINGS, output_type=TopicCheck)


@input_guardrail(run_in_parallel=False)
async def on_topic(context: RunContextWrapper[Scope], agent: Agent, message) -> GuardrailFunctionOutput:
    """Stops off-topic messages before the Clarifier spends anything. If the check itself fails, the
    message goes through: a broken check shouldn't block a real question."""
    try:
        result = await Runner.run(topic_checker, message, hooks=context.context.run.hooks)
        check = result.final_output
    except Exception:
        log.exception("topic check failed: letting the question through")
        check = TopicCheck()
    return GuardrailFunctionOutput(output_info=check, tripwire_triggered=not check.on_topic)


clarifier = Agent(name="Clarifier", instructions=prompts.CLARIFIER, model=MODEL, model_settings=SETTINGS,
                  tools=[find_pitcher], input_guardrails=[on_topic])

researcher = Agent(name="Researcher", instructions=prompts.RESEARCHER, model=RESEARCHER_MODEL,
                   model_settings=SETTINGS, tools=RESEARCH_TOOLS)

writer = Agent(name="Writer", instructions=prompts.WRITER, model=MODEL, model_settings=WRITER_SETTINGS)

# The Writer again, in a fresh context, fixing what the citation check found
reviser = Agent(name="Writer (revise)", instructions=prompts.WRITER, model=MODEL, model_settings=WRITER_SETTINGS)


def apply_edits(draft: str, reply: str) -> str:
    """Apply the reviser's [{"find", "replace"}] edits. An edit whose text isn't in the report is skipped."""
    m = re.search(r"\[.*\]", reply, re.S)
    try:
        edits = json.loads(m.group(0)) if m else []
    except ValueError:
        return draft
    for e in edits if isinstance(edits, list) else []:
        if isinstance(e, dict) and isinstance(e.get("find"), str) and e["find"] and e["find"] in draft:
            draft = draft.replace(e["find"], str(e.get("replace", "")), 1)
    return draft


async def retried(name: str, call, tries: int = 2):
    """Any failed agent gets one fresh try. Running out of turns isn't retried: its caller decides."""
    for attempt in range(1, tries + 1):
        try:
            return await call()
        except MaxTurnsExceeded:
            raise
        except Exception as e:
            if attempt == tries:
                raise
            log.warning("%s failed, trying again: %s: %s", name, type(e).__name__, str(e)[:300])


async def or_else(run: RunState, name: str, call, fallback, tries: int = 2):
    """A failed agent shouldn't lose the work around it: if it keeps failing, carry on with the fallback."""
    try:
        return await retried(name, call, tries)
    except Exception:
        log.exception("%s failed: carrying on without it", name)
        run.fallbacks.append(name)
        return fallback()


async def spawn_researcher(run: RunState, hooks: ResearchHooks, task: str, focus: str, why: str, wave: int) -> str:
    """What researcher.as_tool() would do: run a Researcher in a fresh context and return its
    findings. Written out so each gets a numbered copy, its own focus, and its own panel column."""
    run.researchers += 1
    rid = run.researchers
    name = f"Researcher {rid}"
    agent = researcher.clone(name=name, instructions=prompts.RESEARCHER + "\n\n# Your focus\n" + prompts.FOCUS.get(focus, ""))
    run.emit({"type": "researcher", "id": rid, "name": name, "wave": wave, "focus": focus, "task": task,
              "why": why, "status": "running"})
    started = time.time()
    scope = Scope(run, name)
    reason = ""
    salvaged = False
    log.info("%s started | round %s | %s | %s", name, wave, focus, task[:300])
    try:
        # A retry keeps the same scope, so it shares this researcher's query budget
        result = await retried(name, lambda: Runner.run(agent, f"Task: {task}", context=scope, hooks=hooks,
                                                        max_turns=RESEARCHER_TURNS))
        text = str(result.final_output).strip()
        status = "done"
    except MaxTurnsExceeded as e:
        # Out of turns, but the queries it ran are real and in the ledger: make one last call, with no
        # tools, that writes the findings from the results already in its context.
        log.warning("%s hit its turn limit (%d model calls) | %d ok queries, %d failed: wrapping up",
                    name, RESEARCHER_TURNS, scope.queries, scope.failed)
        text = await wrap_up(agent, e, hooks)
        if text:
            status, salvaged = "done", True
        else:
            reason = f"ran out of turns ({RESEARCHER_TURNS} model calls) and couldn't write findings"
            status = "failed"
            log.error("%s failed: %s", name, reason)
    except Exception as e:  # one failed researcher shouldn't sink the run
        reason = f"{type(e).__name__}: {str(e)[:300]}"
        status = "failed"
        log.exception("%s failed: %s", name, reason)
    if status == "failed":
        text = f"FINDINGS:\n- (no findings: the researcher {reason})\nGAPS: the whole task"
    gaps = m.group(1).strip() if (m := re.search(r"GAPS:\s*(.+)", text, re.S)) else ""
    seconds = round(time.time() - started, 1)
    if status == "done":
        log.info("%s done in %ss | %d ok queries, %d failed%s", name, seconds, scope.queries, scope.failed,
                 " | findings written after the turn limit" if salvaged else "")
    run.findings.append({"id": rid, "name": name, "task": task, "focus": focus, "wave": wave, "why": why, "text": text,
                         "status": status, "reason": reason, "seconds": seconds, "queries": scope.queries,
                         "failed_queries": scope.failed, "salvaged": salvaged})
    run.emit({"type": "researcher", "id": rid, "name": name, "wave": wave, "focus": focus, "task": task, "why": why,
              "status": status, "reason": reason, "findings": text, "gaps": gaps, "seconds": seconds,
              "queries": scope.queries, "failed_queries": scope.failed, "salvaged": salvaged})
    return f"{name} ({focus}) findings:\n{text}"


WRAP_UP = ("You have used all your turns. Don't call any more tools. Write your findings now, in the required "
           "format, from the query results above. Cite only [Q#] ids that appear in those results, and list "
           "what you didn't get to under GAPS.")


async def wrap_up(agent: Agent, error: MaxTurnsExceeded, hooks: "ResearchHooks") -> str:
    """The researcher's conversation so far plus one instruction, answered by the same agent with
    its tools removed. Returns its findings, or "" if that fails too."""
    data = error.run_data
    if data is None:
        return ""
    so_far = [{"role": "user", "content": data.input}] if isinstance(data.input, str) else list(data.input)
    so_far += [item.to_input_item() for item in data.new_items]
    try:
        result = await Runner.run(agent.clone(tools=[]), so_far + [{"role": "user", "content": WRAP_UP}],
                                  hooks=hooks, max_turns=1)
        text = str(result.final_output).strip()
    except Exception:
        log.exception("%s: wrap-up after the turn limit failed", agent.name)
        return ""
    return text if "FINDINGS" in text.upper() else ""


def make_lead(hooks: ResearchHooks) -> Agent:
    @function_tool
    async def research(ctx: RunContextWrapper[Scope], task: str, focus: Focus, why: str = "") -> str:
        """Send a Researcher to answer one task from the database. Call it several times in one turn to
        run Researchers in parallel. Returns their findings, each citing query ids like [Q7].

        Args:
            task: One specific task: pitcher name and MLBAM id, seasons or dates, what to measure.
            focus: The angle: stuff, results_luck, health_workload, history_context or league_scan.
            why: For a follow-up round: the gap in earlier findings this task fills. Empty in round 1.
        """
        run = ctx.context.run
        elapsed = time.time() - run.started
        if run.researchers >= MAX_RESEARCHERS:
            return "No researchers left. Write your brief for the writer now."
        wave = run.wave_of_turn.get(run.lead_turns)
        if wave is None:
            wave = len(run.wave_of_turn) + 1
            if wave > MAX_ROUNDS:
                return f"The round limit ({MAX_ROUNDS}) is reached. Write your brief for the writer now."
            if wave > 1 and elapsed > FOLLOWUP_UNTIL or elapsed > RESEARCH_UNTIL:
                run.emit({"type": "budget", "seconds": round(elapsed)})
                return "The time budget for research is used up. Write your brief for the writer now."
            run.wave_of_turn[run.lead_turns] = wave
            run.emit({"type": "wave", "wave": wave, "followup": wave > 1})
        return await spawn_researcher(run, hooks, task, focus, why, wave)

    return Agent(name="Lead", instructions=prompts.LEAD, model=MODEL, model_settings=SETTINGS,
                 tools=[find_pitcher, pitcher_overview, research])


# --- Clarification ---


class ClarifyQuestion(BaseModel):
    question: str
    options: list[str] = Field(default_factory=list)


class PitcherRef(BaseModel):
    name: str
    mlbam_id: int | None = None


class Comparison(BaseModel):
    a_start: str
    a_end: str
    b_start: str
    b_end: str


class Clarification(BaseModel):
    status: Literal["clear", "clarify", "out_of_scope", "off_topic"] = "clear"
    question: str = ""
    pitchers: list[PitcherRef] = Field(default_factory=list)
    seasons: list[int] = Field(default_factory=list)
    compare: Comparison | None = None
    unanswerable: list[str] = Field(default_factory=list)
    clarifying_questions: list[ClarifyQuestion] = Field(default_factory=list)
    message: str = ""


def comparison_of(c: Clarification) -> dict:
    """The two periods a question compares: the Clarifier's, or else its last two seasons."""
    if c.compare:
        return c.compare.model_dump()
    later = max(c.seasons, default=2026)
    earlier = max((s for s in c.seasons if s < later), default=later - 1)
    return {"a_start": f"{earlier}-01-01", "a_end": f"{earlier}-12-31", "b_start": f"{later}-01-01", "b_end": f"{later}-12-31"}


def parse_clarification(text: str, question: str) -> Clarification:
    """The Clarifier replies with JSON. Tolerate code fences and stray prose; on failure, research
    the question as asked rather than fail the run."""
    m = re.search(r"\{.*\}", text, re.S)
    try:
        c = Clarification.model_validate(json.loads(m.group(0))) if m else None
    except (ValueError, ValidationError):
        c = None
    if c is None:
        return Clarification(status="clear", question=question, message="")
    if not c.question:
        c.question = question
    return c


async def clarify(question: str, answers: list[dict], rounds: int, run: RunState, hooks: ResearchHooks) -> Clarification:
    prompt = f"Question: {question}"
    if answers:
        prompt += "\n\nThe user already answered:\n" + "\n".join(f"- {a.get('question')}: {a.get('answer')}" for a in answers)
    try:
        result = await Runner.run(clarifier, prompt, context=Scope(run, "Clarifier"), hooks=hooks, max_turns=6)
    except InputGuardrailTripwireTriggered as e:
        return Clarification(status="off_topic", question=question, message=e.guardrail_result.output.output_info.message)
    c = parse_clarification(str(result.final_output), question)
    if c.status == "clarify" and (rounds >= MAX_CLARIFY_ROUNDS or not c.clarifying_questions):
        c.status = "clear"  # asked enough: research the best reading
    if c.status == "out_of_scope" and answers:
        c.status = "clear"  # the user chose what to research instead
    return c


# --- The run ---


def _gap(text: str) -> str:
    m = re.search(r"GAPS:\s*(.+)", text, re.S)
    return m.group(1).strip()[:400] if m else ""


def _has_gap(text: str) -> bool:
    g = _gap(text).lower().strip(" .")
    return bool(g) and g not in ("none", "n/a", "no gaps")


def _findings_block(run: RunState) -> str:
    parts = []
    for f in sorted(run.findings, key=lambda f: f["id"]):
        head = f"## {f['name']} — {f['focus']} (round {f['wave']})\nTask: {f['task']}"
        if f["why"]:
            head += f"\nGap it fills: {f['why']}"
        parts.append(head + "\n\n" + f["text"])
    return "\n\n---\n\n".join(parts)


def findings_report(run: RunState) -> str:
    """When the Writer can't write: the Researchers' findings as they are, so the run's work isn't lost."""
    return ("> **The Writer failed, so this is the Researchers' findings, unedited, with no verdict.**\n\n"
            + _findings_block(run))


DEFAULT_WAVE = [
    ("stuff", "Compare {who}'s pitch arsenal, velocity and movement across seasons; find what changed."),
    ("results_luck", "Separate skill from luck for {who}: K%, BB%, whiff% vs wOBA-xwOBA, BABIP, HR/FB, ERA vs FIP."),
    ("health_workload", "Check {who}'s injured list stints and roster moves, and velocity/pitch counts around them."),
]


def run_settings() -> dict:
    """What a run was configured with, so two saved runs can be compared fairly."""
    return {"lead_model": MODEL.model, "researcher_model": RESEARCHER_MODEL.model,
            "writer_effort": WRITER_SETTINGS.extra_args.get("reasoning_effort"),
            "max_researchers": MAX_RESEARCHERS, "max_rounds": MAX_ROUNDS,
            "followup_until": FOLLOWUP_UNTIL, "research_until": RESEARCH_UNTIL,
            "git_commit": git_commit(),
            "prompt_hash": prompt_hash(prompts.CLARIFIER, prompts.LEAD, prompts.RESEARCHER, prompts.WRITER,
                                       prompts.REVISE, *prompts.FOCUS.values())}


def run_record(run_id: str, question: str, answers: list[dict], clarify_rounds: int, force: bool, run: RunState,
               hooks: ResearchHooks, status: str, result: dict | None = None, error: str = "") -> dict:
    """Everything worth keeping about a run. It reads the run's state, not just its result, so a run
    that failed part-way keeps the work it finished."""
    repeated = ("evidence", "findings", "failed_queries")  # in the record already, from the run's state
    return {"run_id": run_id, "status": status, "error": error, "question": question, "answers": answers,
            "clarify_rounds": clarify_rounds, "force": force,
            "started_at": dt.datetime.fromtimestamp(run.started).astimezone().isoformat(timespec="seconds"),
            "seconds": round(time.time() - run.started, 1), "settings": run_settings(),
            "tokens": hooks.tokens, "tokens_by_agent": hooks.tokens_by_agent, "timings": run.timings,
            "fallbacks": run.fallbacks, "pitchers": run.pitchers,
            "chance_check": run.chance_check, "unanswerable": run.unanswerable, "brief": run.brief, "draft": run.draft, "findings": run.findings,
            "query_log": run.query_log, "failed_queries": run.failed_queries, "evidence": run.ledger.public(),
            "result": {k: v for k, v in (result or {}).items() if k not in repeated}}


async def deep_research(question: str, answers: list[dict], contexts: AgentContexts, emit,
                        clarify_rounds: int = 0, force: bool = False, run_id: str = "") -> dict:
    """Run the research and save a record of it, however it ends."""
    run_id = run_id or uuid.uuid4().hex
    current_run.set(run_id[:8])
    log.info("run started | %r | answers=%d clarify_round=%d force=%s", question[:300], len(answers),
             clarify_rounds, force)
    hooks = ResearchHooks(contexts, emit)
    run = RunState(emit=emit)
    run.started = time.time()
    hooks.run = run
    run.hooks = hooks
    args = (run_id, question, answers, clarify_rounds, force, run, hooks)
    try:
        result = await research_run(question, answers, clarify_rounds, force, run, hooks)
    except asyncio.CancelledError:  # the page closed the stream
        save_run(run_record(*args, status="stopped"))
        raise
    except Exception as e:
        save_run(run_record(*args, status="error", error=f"{type(e).__name__}: {str(e)[:500]}"))
        raise
    save_run(run_record(*args, status=result["type"], result=result))
    return result


async def research_run(question: str, answers: list[dict], clarify_rounds: int, force: bool, run: RunState,
                       hooks: ResearchHooks) -> dict:
    started = run.started
    emit = run.emit
    timings = run.timings
    mark = time.time()

    def lap(name: str) -> None:
        nonlocal mark
        timings[name] = round(time.time() - mark, 1)
        mark = time.time()

    # 1. Clarify: is the question specific enough?
    emit({"type": "phase", "phase": "clarify"})
    c = await or_else(run, "Clarifier", lambda: clarify(question, answers, clarify_rounds, run, hooks),
                      lambda: Clarification(status="clear", question=question))
    # force is the user asking to research a vague question as it stands. It can't override what can't be researched.
    if c.status != "clear" and (c.status != "clarify" or not force):
        log.info("clarify: %s | %s | %d question(s)", c.status, c.message[:200], len(c.clarifying_questions))
        return {"type": "clarify", "status": c.status, "message": c.message, "restated": c.question,
                "questions": [q.model_dump() for q in c.clarifying_questions][:3]}
    for p in c.pitchers:
        if p.mlbam_id is None and (hit := lookup_pitchers(p.name)):
            p.mlbam_id = hit[0]["mlbam_id"]
        if p.mlbam_id:
            run.pitchers[p.mlbam_id] = p.name
            emit({"type": "pitcher", "mlbam_id": p.mlbam_id, "name": p.name})
    run.chance_check = chance_checks(run, comparison_of(c))
    run.unanswerable = c.unanswerable
    lap("clarify")
    emit({"type": "brief", "question": c.question, "message": c.message,
          "pitchers": [p.model_dump() for p in c.pitchers], "seasons": c.seasons})

    # 2. Research: plan, fan out, follow up on gaps
    emit({"type": "phase", "phase": "research"})
    lead = make_lead(hooks)
    who = ", ".join(f"{p.name} (id {p.mlbam_id})" for p in c.pitchers) or "the pitchers in question"
    lead_prompt = (f"Question: {c.question}\nOriginal wording: {question}\nPitchers: {who}\n"
                   f"Seasons: {', '.join(map(str, c.seasons)) or 'not specified (default: 2026 vs 2025)'}"
                   + (f"\n\nChance check for the periods in the question:\n{run.chance_check}" if run.chance_check else "")
                   + (f"\n\nThe data can't answer: {'; '.join(run.unanswerable)}" if run.unanswerable else ""))
    brief = ""
    lead_scope = Scope(run, "Lead")
    try:
        result = await Runner.run(lead, lead_prompt, context=lead_scope, hooks=hooks, max_turns=12)
        brief = str(result.final_output)
        # Gap review: after each round, show the Lead the gaps its researchers reported, in its own
        # conversation. It sends a follow-up round for the ones the data can answer, or declines.
        while len(run.wave_of_turn) < MAX_ROUNDS and time.time() - started < FOLLOWUP_UNTIL:
            last = max(run.wave_of_turn.values(), default=0)
            gaps = [f for f in run.findings if f["wave"] == last and _has_gap(f["text"])]
            if not gaps:
                break
            listed = "\n".join(f"- {f['name']} ({f['focus']}): {_gap(f['text'])}" for f in gaps)
            emit({"type": "gap_review", "round": last, "gaps": [{"name": f["name"], "gap": _gap(f["text"])} for f in gaps]})
            review = (f"Gap review after round {last}. Your researchers reported these gaps:\n{listed}\n\n"
                      "For each, decide whether our data can answer it (see the data scope). If any can and it "
                      "matters for the verdict, call `research` now for just those, in one turn, with `why` naming "
                      "the gap. If none can, reply with your brief, starting with 'NO FOLLOW-UP:' and one line on why.")
            rounds_before = len(run.wave_of_turn)
            result = await Runner.run(lead, result.to_input_list() + [{"role": "user", "content": review}],
                                      context=lead_scope, hooks=hooks, max_turns=8)
            brief = str(result.final_output)
            log.info("gap review after round %d: %d researcher(s) reported gaps", last, len(gaps))
            if len(run.wave_of_turn) == rounds_before:
                reason = brief.split("NO FOLLOW-UP:", 1)[-1].strip().splitlines()[0][:240] if brief.strip() else ""
                emit({"type": "gap_decision", "round": last, "followup": False, "reason": reason})
                break
            emit({"type": "gap_decision", "round": last, "followup": True})
    except Exception:  # whatever stopped the Lead, the findings it already has are still good
        log.exception("Lead failed; carrying on with the findings so far")
        run.fallbacks.append("Lead")
        brief = brief or "(The Lead stopped early; write from the findings.)"
    if not run.findings:
        log.warning("Lead delegated nothing; running the default first round")
        # The Lead answered without delegating: run the standard first round ourselves
        emit({"type": "wave", "wave": 1, "followup": False})
        await asyncio.gather(*(spawn_researcher(run, hooks, t.format(who=who), f, "", 1) for f, t in DEFAULT_WAVE))
    if all(f["status"] == "failed" for f in run.findings):
        raise RuntimeError("No researcher finished, so there's nothing to write a report from.")

    lap("research")
    run.brief = brief

    # 3. Write: one call, outside the loop. The Writer sees only the question, the brief and the findings.
    emit({"type": "phase", "phase": "write"})
    writer_input = f"Question: {c.question}\n\nLead's brief:\n{brief}\n\nFindings:\n\n{_findings_block(run)}"
    if run.chance_check:
        writer_input += f"\n\nChance check for the periods in the question, which you can cite:\n\n{run.chance_check}"
    if run.unanswerable:
        writer_input += f"\n\nThe data can't answer: {'; '.join(run.unanswerable)}"

    async def write() -> str:
        return str((await Runner.run(writer, writer_input, hooks=hooks)).final_output).strip()

    draft = await or_else(run, "Writer", write, lambda: findings_report(run))
    run.draft = draft

    lap("write")

    # 4. Verify the citations in code; the Writer fixes what fails, once, as find/replace edits
    # (rewriting the whole report would double the Writer's time).
    emit({"type": "phase", "phase": "verify"})
    check = verify(draft, run.ledger)
    first_check = check
    if check["issues"]:
        emit({"type": "verify", "stage": "draft", **check})
        cited = sorted({q for u in check["unmatched"] for q in u["cited"]}, key=lambda q: int(q[1:]))
        rows = "\n\n".join(as_markdown(run.ledger.get(q)) for q in cited[:8] if run.ledger.get(q))
        revise_input = ("# The report\n" + draft + "\n\n" + prompts.REVISE.format(issues=issues_for_writer(check)) +
                        (f"\n\nRows of the queries it cites:\n\n{rows}" if rows else ""))

        async def revise() -> str:
            return str((await Runner.run(reviser, revise_input, hooks=hooks)).final_output)

        # The fix is optional, so one try: without it, the draft stands and the page flags what didn't match
        reply = await or_else(run, "Writer (revise)", revise, lambda: "", tries=1)
        revised = apply_edits(draft, reply)
        revised_check = verify(revised, run.ledger)
        if revised != draft and revised_check["issues"] < check["issues"]:
            draft, check = revised, revised_check
    lap("verify")
    stats = {"seconds": round(time.time() - started, 1), "researchers": run.researchers,
             "rounds": max([f["wave"] for f in run.findings] or [0]), "queries": len(run.ledger.public()),
             "failed_queries": len(run.failed_queries),
             "failed_researchers": sum(1 for f in run.findings if f["text"].startswith("FINDINGS:\n- (no findings")),
             "tokens": hooks.tokens, "timings": timings, "fallbacks": run.fallbacks}
    log.info("run done | %ss | %d researchers, %d rounds, %d queries (%d failed) | citations %s/%s, %d issue(s) "
             "(draft %d) | tokens %d in, %d out, %d model calls | timings %s", stats["seconds"], stats["researchers"],
             stats["rounds"], stats["queries"], stats["failed_queries"], check["numbers_matched"],
             check["numbers_checked"], check["issues"], first_check["issues"], hooks.tokens["input"],
             hooks.tokens["output"], hooks.tokens["calls"], timings)
    for u in check["unmatched"]:
        log.warning("unverified number %s (cites %s): %s", u["number"], ",".join(u["cited"]), u["sentence"][:200])

    return {
        "type": "done",
        "markdown": draft,
        "question": c.question,
        "evidence": run.ledger.public(),
        "verification": {**check, "draft_issues": first_check["issues"], "revised": first_check is not check},
        "pitchers": [{"mlbam_id": k, "name": v} for k, v in run.pitchers.items()],
        "findings": run.findings,
        "failed_queries": run.failed_queries,
        "stats": stats,
    }
