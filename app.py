"""Pitch Lab: deep research on why MLB pitchers get better or worse.

    uv run app.py        then open http://localhost:8000

Streams each research run to the page as Server-Sent Events (/run), answers the same research as
one JSON reply with every agent's tool calls (POST /chat), serves the pitcher dashboard's data, and
the context panel (every agent's exact context, live).
"""

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from agents import set_tracing_disabled
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")  # VERTEXAI_PROJECT etc. Before importing the agents, which read env vars.

from agent.evidence import DECIMALS, connection  # noqa: E402
from agent.logs import setup as setup_logging  # noqa: E402
from agent.pipeline import deep_research  # noqa: E402
from agent.thread import Thread  # noqa: E402
from agent.tools import lookup_pitchers  # noqa: E402
from context_panel import AgentContexts, add_context_panel  # noqa: E402
from data_pipeline.db import ensure_db  # noqa: E402

log = setup_logging()

# Traces upload to OpenAI by default, which needs an OpenAI key. We're on Gemini.
set_tracing_disabled(True)

# Local file, else build from Parquet, else download from GCS (see data_pipeline/db.py)
ensure_db()

# run_id -> every agent's context, in order. In-memory, single process. The context panel reads it.
sessions: dict[str, AgentContexts] = {}
MAX_SESSIONS = 50

app = FastAPI(title="Pitch Lab")
add_context_panel(app, sessions)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
def index():
    # no-cache: browsers otherwise keep serving an old page after a redeploy
    return FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
def healthz():
    n = connection().cursor().execute("SELECT count(*) FROM pitcher_season").fetchone()[0]
    return {"ok": True, "pitcher_seasons": n}


@app.get("/run")
async def run(question: str, answers: str = "[]", rounds: int = 0, force: bool = False, session_id: str = ""):
    """Research one question and stream what happens as Server-Sent Events.
    answers: JSON list of {question, answer} from earlier clarification rounds.
    session_id: the conversation to continue, for a follow-up. Empty starts a new one."""
    try:
        parsed = json.loads(answers)
        assert isinstance(parsed, list)
    except (ValueError, AssertionError):
        raise HTTPException(400, "answers must be a JSON list")
    run_id = str(uuid.uuid4())
    if len(sessions) >= MAX_SESSIONS:
        sessions.pop(next(iter(sessions)))
    sessions[run_id] = AgentContexts()
    queue: asyncio.Queue = asyncio.Queue()
    session_id, session = conversation(session_id)
    followup_of = len(session.thread.turns)

    async def work():
        try:
            async with session.lock:
                result = await deep_research(question.strip()[:600], parsed[:10], sessions[run_id], queue.put_nowait,
                                             clarify_rounds=rounds, force=force, run_id=run_id, thread=session.thread)
            queue.put_nowait(result)
        except asyncio.CancelledError:
            log.info("run %s stopped: the page closed the stream", run_id[:8])
            raise
        except Exception as e:
            log.exception("run %s failed", run_id[:8])
            # Auth, quota, a model that won't stop: show it on the page, not as a dropped stream.
            queue.put_nowait({"type": "error", "message": f"{type(e).__name__}: {str(e)[:500]}"})

    task = asyncio.create_task(work())

    async def events():
        try:
            yield f"data: {json.dumps({'type': 'start', 'run_id': run_id, 'session_id': session_id, 'followup_of': followup_of})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # proxies (Cloud Run) close idle streams
                    continue
                yield f"data: {json.dumps(event, default=str)}\n\n"
                if event["type"] in ("done", "error", "clarify"):
                    break
        finally:
            task.cancel()  # the page closed the stream: stop spending tokens

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --- /chat: the same research as one JSON reply, in Project 1's shape ---


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    new_session: bool = False  # forget this conversation and treat message as a new question
    force: bool = False  # research a vague question as asked instead of asking back


class ChatResponse(BaseModel):
    response: str  # the report (Markdown), the clarifying question, or why it can't answer
    session_id: str  # send it back to follow up on this answer
    # Every tool call in the run, by every agent: {"agent", "name", "args", "result"}. The Lead's
    # `research` calls are its Researchers; each Researcher's own calls carry its name.
    tool_calls: list[dict]
    status: str  # done | clarify | out_of_scope | off_topic | error
    run_id: str
    followup_of: int = 0  # how many earlier answers in this conversation this one had as context
    clarifying_questions: list[dict] = []  # when status is clarify: {"question", "options"}
    evidence: dict = {}  # Q id -> the SQL, rows and source behind each [Q#] citation
    verification: dict = {}  # the code's citation check
    stats: dict = {}


@dataclass
class ChatSession:
    """A conversation: its finished questions (thread), and the one being clarified."""
    thread: Thread = field(default_factory=Thread)
    question: str = ""
    answers: list[dict] = field(default_factory=list)
    rounds: int = 0
    pending: list[dict] = field(default_factory=list)  # clarifying questions waiting for a reply
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


chats: dict[str, ChatSession] = {}


def conversation(session_id: str | None) -> tuple[str, ChatSession]:
    """The conversation with this id, or a new one. In memory: if the server restarts, a follow-up
    starts over, with followup_of 0 in its reply. Past MAX_SESSIONS the least recently used is dropped."""
    session_id = session_id or str(uuid.uuid4())
    session = chats.pop(session_id, None) or ChatSession()
    if len(chats) >= MAX_SESSIONS:
        chats.pop(next(iter(chats)))
    chats[session_id] = session
    return session_id, session


def clarification_text(result: dict) -> str:
    lines = [result.get("message") or "I need a bit more detail before researching this."]
    for i, q in enumerate(result.get("questions", []), 1):
        lines.append(f"\n{i}. {q['question']}")
        lines += [f"   - {o}" for o in q.get("options", [])]
    if result["status"] == "clarify":
        lines.append("\nReply with your answer, or send the question again with \"force\": true to research it as asked.")
    elif result.get("questions"):
        lines.append("\nReply with one of these to research it instead.")
    return "\n".join(lines)


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Research a question and return the report with every tool call. Usually takes 1.5-3 minutes.
    If the question is vague, the reply asks back (status "clarify"); send the answer with the same
    session_id and the research continues from the original question. After a finished answer, the
    next message with that session_id is a follow-up: the agents get the earlier reports, and new
    citations keep counting up from the earlier ones."""
    message = request.message.strip()[:600]
    if not message:
        raise HTTPException(400, "message is empty")
    session_id, session = conversation(request.session_id)
    async with session.lock:
        if request.new_session:
            session.thread, session.pending = Thread(), []
        if session.pending:
            # The message answers the clarifying questions: research the original question with it
            asked = " / ".join(q["question"] for q in session.pending)
            session.answers.append({"question": asked, "answer": message})
            session.rounds += 1
        else:
            session.question, session.answers, session.rounds = message, [], 0
        session.pending = []
        followup_of = len(session.thread.turns)
        run_id = str(uuid.uuid4())
        if len(sessions) >= MAX_SESSIONS:
            sessions.pop(next(iter(sessions)))
        sessions[run_id] = AgentContexts()  # so the context panel can show a /chat run too
        tool_calls: list[dict] = []
        try:
            result = await deep_research(session.question, session.answers[:10], sessions[run_id], lambda event: None,
                                         clarify_rounds=session.rounds, force=request.force, run_id=run_id,
                                         tool_calls=tool_calls, thread=session.thread)
        except Exception as e:
            log.exception("chat run %s failed", run_id[:8])
            return ChatResponse(response=f"The research failed: {type(e).__name__}: {str(e)[:300]}. "
                                         "The tool calls it made before failing are below.",
                                session_id=session_id, tool_calls=tool_calls, status="error", run_id=run_id, followup_of=followup_of)
        if result["type"] == "clarify":
            if result["status"] in ("clarify", "out_of_scope"):  # off_topic asks nothing back
                session.pending = result.get("questions", [])
            return ChatResponse(response=clarification_text(result), session_id=session_id, tool_calls=tool_calls,
                                status=result["status"], run_id=run_id, followup_of=followup_of,
                                clarifying_questions=result.get("questions", []))
        return ChatResponse(response=result["markdown"], session_id=session_id, tool_calls=tool_calls, status="done",
                            run_id=run_id, followup_of=followup_of, evidence=result["evidence"], verification=result["verification"],
                            stats=result["stats"])


# --- Dashboard data: straight from the database, no model involved ---


def _rows(sql: str, params: list) -> list[dict]:
    cur = connection().cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        row = {}
        for c, v in zip(cols, r):
            row[c] = v.isoformat()[:10] if hasattr(v, "isoformat") else (round(v, DECIMALS) if isinstance(v, float) else v)
        out.append(row)
    return out


@app.get("/api/search")
def search(q: str):
    if len(q.strip()) < 2:
        return []
    return [{k: f.get(k) for k in ("mlbam_id", "name", "last_team", "throws", "position", "statcast_seasons", "pitches")}
            for f in lookup_pitchers(q)]


@app.get("/api/pitcher/{mlbam_id}")
def pitcher(mlbam_id: int):
    seasons = _rows("""SELECT season, name, pitches, batters_faced, k_pct, bb_pct, whiff_pct, chase_pct, csw_pct,
                              avg_velo_fastball, woba, xwoba, woba_minus_xwoba, babip, hr_per_fb, run_value_total
                       FROM pitcher_season WHERE pitcher = ? ORDER BY season""", [mlbam_id])
    if not seasons:
        raise HTTPException(404, "No Statcast data for this pitcher (2024-2026).")
    return {
        "mlbam_id": mlbam_id,
        "profile": (_rows("""SELECT full_name, birth_date, pitch_hand, primary_position, mlb_debut_date
                             FROM mlb_people WHERE mlbam_id = ?""", [mlbam_id]) or [{}])[0],
        "team": (_rows("""SELECT arg_max(CASE WHEN inning_topbot = 'Top' THEN home_team ELSE away_team END, game_date) AS team
                          FROM pitches WHERE pitcher = ?""", [mlbam_id]) or [{}])[0].get("team"),
        "seasons": seasons,
        "months": _rows("""SELECT season, game_month, pitches, avg_velo_fastball, whiff_pct, csw_pct, xwoba, k_pct
                           FROM pitcher_month WHERE pitcher = ? ORDER BY season, game_month""", [mlbam_id]),
        "arsenal": _rows("""SELECT season, pitch_type, pitches, usage_pct, avg_velo, avg_spin, avg_pfx_x_in,
                                   avg_pfx_z_in, whiff_pct, xwoba, run_value_total
                            FROM pitcher_season_pitch_type WHERE pitcher = ? AND pitches >= 20
                            ORDER BY season, pitches DESC""", [mlbam_id]),
        "career": _rows("""SELECT s.season, s.team, s.age, round(s.ip_decimal, 1) AS ip, s.ERA, s.FIP, s.ERA_plus, v.WAR
                           FROM bref_pitching_standard s
                           LEFT JOIN bref_pitching_value v ON v.season = s.season AND v.bref_id = s.bref_id
                           WHERE s.mlbam_id = ? ORDER BY s.season""", [mlbam_id]),
        "il": _rows("""SELECT season, il_type, start_date, end_date, days_on_il, injury_note, placement_description
                       FROM il_stints WHERE mlbam_id = ? ORDER BY start_date""", [mlbam_id]),
        "links": {
            "savant": f"https://baseballsavant.mlb.com/savant-player/{mlbam_id}",
            "mlb": f"https://www.mlb.com/player/{mlbam_id}",
        },
    }


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 8000)))
