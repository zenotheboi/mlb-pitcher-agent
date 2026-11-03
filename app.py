"""Pitch Lab: deep research on why MLB pitchers get better or worse.

    uv run app.py        then open http://localhost:8000

Streams each research run to the page as Server-Sent Events, serves the pitcher dashboard's
data, and the context panel (every agent's exact context, live).
"""

import asyncio
import json
import os
import uuid
from pathlib import Path

import uvicorn
from agents import set_tracing_disabled
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")  # VERTEXAI_PROJECT etc. Before importing the agents, which read env vars.

from agent.evidence import connection  # noqa: E402
from agent.logs import setup as setup_logging  # noqa: E402
from agent.pipeline import deep_research  # noqa: E402
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
async def run(question: str, answers: str = "[]", rounds: int = 0, force: bool = False):
    """Research one question and stream what happens as Server-Sent Events.
    answers: JSON list of {question, answer} from earlier clarification rounds."""
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

    async def work():
        try:
            result = await deep_research(question.strip()[:600], parsed[:10], sessions[run_id], queue.put_nowait,
                                         clarify_rounds=rounds, force=force, run_id=run_id)
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
            yield f"data: {json.dumps({'type': 'start', 'run_id': run_id})}\n\n"
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


# --- Dashboard data: straight from the database, no model involved ---


def _rows(sql: str, params: list) -> list[dict]:
    cur = connection().cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        row = {}
        for c, v in zip(cols, r):
            row[c] = v.isoformat()[:10] if hasattr(v, "isoformat") else (round(v, 4) if isinstance(v, float) else v)
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
