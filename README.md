# Pitch Lab (mlb-pitcher-agent)

A Deep Research agent for one question: *why did an MLB pitcher get better or worse, and is the change
real skill, luck, or injury?* It answers from the 2024–2026 pitch-by-pitch data, Baseball Reference
season lines and the injured list, and every number in its report links to the query that produced it.

**Who it's for:** fantasy managers, bettors, analysts and fans who see a pitcher's ERA jump or drop
and want to know whether it will last. A surface stat like ERA moves with luck: balls in play
finding holes, or runners scoring at the wrong moment. The underlying numbers (strikeouts, whiffs,
expected stats, velocity) show whether the pitcher himself changed. Pitch Lab checks both, measures each
change against normal noise, and says which one moved.

**Data:** Baseball Savant's Statcast pitch-level data (2.14M regular-season pitches, 2024–2026),
Baseball Reference season lines (2015–2026, saved by hand because the site blocks scripts), the MLB
Stats API's injured list and transactions (2024–2026), and the Chadwick register to join their player
IDs. Our scripts in `data_pipeline/` fetch them and build one read-only DuckDB file, kept in a GCS
bucket that the app downloads on first start. See [Get the database](#get-the-database) and
[What data there is](#what-data-there-is).

**Live app:** TODO (Cloud Run URL, also in [`submission.json`](submission.json))
**Path:** Deep Research (Lead, parallel Researchers, Writer, plus a Clarifier and a Critic)

**How to use it:** type a question that names a pitcher and what changed, or click an example. If the
question is vague ("Is Smith for real?"), it asks which pitcher or angle first. A run usually takes
1.5 to 3 minutes. The page shows the agents working live, and the report appears as the Writer writes it.
Click any `[Q#]` citation to see the rows behind it and a link to the upstream source. The page doesn't
show SQL; each query's exact SQL is in `/chat`'s `tool_calls` and `evidence`, and in the run record. The same research is available as JSON from [`POST /chat`](#post-chat).

**What it can't answer**, and says so when asked: Stuff+, Location+, xFIP or SIERA (FanGraphs models),
medical details, mechanics or coaching notes, the minors, spring training and the postseason.
Pitch-level data starts in 2024.

## Sample queries

Run on 2026-11-05 (Gemini 3.6 Flash for the agents). "No tools" is the same model the agents use, `gemini-3.6-flash` on
Vertex AI, given only the question: no tools and no system prompt. Full outputs are in
[`docs/samples/`](docs/samples/).

### 1. "Why did Gavin Williams get worse in 2026? His ERA rose from 3.06 to 3.76."

**Expected:** the premise is misleading. ERA rose (3.06 → 3.76), but he pitched better: FIP
4.39 → 3.48, K% 24.6% → 33.2%, BB% 11.8% → 8.2%, whiff% 27.7% → 32.8%, xwOBA .323 → .301, no IL time. The ERA
rise is BABIP (.254 → .288) and when the hits came.
**Source:** `pitcher_season` (Statcast), `bref_pitching_standard` (ERA, FIP), `il_stints`.

| No tools | Pitch Lab |
|---|---|
| Doesn't know the 2026 season ("the 2026 MLB season is either in progress, projected, or part of a baseball simulation"). It accepts the premise and explains a decline from his general profile: "High walk rate", "Over-reliance on the 4-seam fastball", LOB% and BABIP "regression". It invents typical numbers, such as FIP "around the 3.80–4.20 range" and "HR/9 rising from 0.8 to 1.2". | "Gavin Williams did not actually pitch worse in 2026." FIP 4.39 → 3.48, K% 24.6% → 33.2% (z = 3.7), BB% 11.8% → 8.2%, xwOBA .323 → .301, and BABIP from .254 to .288. Four-seamer +1.0 mph. No injuries in 2025–26. **80/80 numbers match their cited queries**, from 22 queries by 4 Researchers in 2 rounds. Verdict label "Mixed: skill + luck" (the test set expects "Real skill gain"; the text says the skill gain). [Full JSON](docs/samples/chat_gavin_williams.json) |

### 2. "Was Tarik Skubal's 2026 as good as 2025 under the hood, or did luck change?"

**Expected:** mostly luck. ERA 2.21 → 2.72, but FIP 2.45 → 2.51, xwOBA .258 → .266, K% 32.2% → 30.0%,
BB% 4.4% both years. wOBA went from beating xwOBA (.247) to trailing it (.274). Fastball velocity (FF/SI/FC blended) 97.4 → 96.8 mph.
43 days on the IL (May 1 – Jun 13, 2026, loose bodies in his left elbow).
**Source:** `pitcher_season`, `bref_pitching_standard`, `il_stints`.

| No tools | Pitch Lab |
|---|---|
| "**The 2025 and 2026 MLB seasons have not occurred.**" It answers a different question (2023 vs 2024) with numbers from memory, and calls his profile "luck-proof". | "Verdict: Mostly luck · Confidence: High · Injury overlap: Yes." ERA 2.21 → 2.72 while xwOBA moved .258 → .266 (z = 0.4) and FIP 2.45 → 2.51. wOBA went from beating xwOBA to trailing it, and BABIP rose .273 → .289. It also finds the 43-day IL stint, the 0.8 mph fastball drop and a zone rate fall (z = -3.9). **73/73 numbers match.** [Full JSON](docs/samples/run_skubal_luck.json) |

### 3. "What was Tarik Skubal's Stuff+ in 2026?"

**Expected:** the data can't answer it, because Stuff+ is a FanGraphs model and isn't in the database.
The agent should give no value and offer what it can measure.
**Source:** none. `docs/schema.md` lists every column, and no Stuff+ is among them.

| No tools | Pitch Lab |
|---|---|
| "The 2026 MLB season has not happened yet… For reference, during his 2024… Skubal posted an overall **Stuff+ of 112**", an unverifiable number. | "FanGraphs Stuff+ is not available in our dataset, but we can analyze Tarik Skubal's pitch quality using Statcast metrics like velocity, movement, whiff rate, and expected xwOBA." It offers two research questions it can answer, and runs no research (status `out_of_scope`). Replying with one of them starts that research in the same session. [Full JSON](docs/samples/chat_skubal_stuff_plus.json) |

## POST /chat

The same research as one JSON reply, in Project 1's `/chat` shape (`response`, `session_id`,
`tool_calls`). Every tool call in the run is listed, by every agent, with an `agent` field. The Lead's
`research` calls are the Researchers it starts, and each Researcher's own calls carry its name.

```bash
curl -X POST localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"message": "Why did Gavin Williams get worse in 2026? His ERA rose from 3.06 to 3.76."}'
```

```jsonc
{
  "response": "> **Verdict: Mixed: skill + luck** · Confidence: High · Injury overlap: No\n\nGavin Williams did not actually pitch worse…",
  "session_id": "1f0c…",
  "tool_calls": [
    {"agent": "Clarifier", "name": "find_pitcher", "args": {"name": "Gavin Williams"}, "result": "…"},
    {"agent": "Chance check (code)", "name": "check_change", "args": {"mlbam_id": 668909, "a_start": "2025-01-01", …}, "result": "[Q1] …"},
    {"agent": "Researcher 1", "name": "pitcher_overview", "args": {"mlbam_id": 668909}, "result": "[Q4] Statcast season lines… | season | k_pct | …"},
    {"agent": "Lead", "name": "research", "args": {"task": "…", "focus": "stuff", "why": "…"}, "result": "Researcher 2 (stuff) findings: …"},
    …
  ],
  "status": "done",            // done | clarify | out_of_scope | off_topic | error
  "run_id": "…",
  "clarifying_questions": [],  // when status is clarify or out_of_scope: [{"question", "options"}]
  "evidence": {"Q4": {"sql": "…", "columns": […], "rows": […], "sources": […]}, …},
  "verification": {"numbers_checked": 80, "numbers_matched": 80, …},
  "stats": {"seconds": 158.5, "researchers": 4, "rounds": 2, "queries": 22, …}
}
```

Request: `{"message": str, "session_id"?: str, "new_session"?: bool, "force"?: bool}`. If the
reply's status is `clarify` (or `out_of_scope` with suggestions), send the answer with the same
`session_id`, and it researches the original question with that answer. `force: true` researches a
vague question as asked. `new_session: true` drops a pending clarification. A call usually takes 1.5 to 3 minutes;
the web page uses the streaming `GET /run` instead, which sends the same steps live.

## Assignment checklist

| Requirement | Where |
|---|---|
| Data the model doesn't know deeply | The 2026 season, pitch by pitch. With no tools, the model says 2026 hasn't happened (samples above) |
| Gets at the data through tools | Read-only SQL over DuckDB: 11 shaped tools plus `run_sql` (`agent/tools.py`) |
| Combines evidence into a supported claim | A verdict (skill / luck / injury overlap) from the chance check's z scores and the findings |
| Says when the data can't answer | Clarifier `out_of_scope` (sample 3), "What the data can't tell us" in every report |
| Shows its work; `/chat` keeps `response`, `session_id`, `tool_calls`, sub-agents' calls carry `agent` | [`POST /chat`](#post-chat); the page's research floor, evidence panel and "Agent contexts" |
| Front end says what it explores, who it's for, how to use it | The top of the page: For / How / Can't |
| Deployed on Cloud Run | [Deploy to Cloud Run](#deploy-to-cloud-run); URL in `submission.json` |
| At least three agent shapes | Clarifier, Lead, Researcher, Writer, Critic |
| Lead fans out to Researchers in one turn, run in parallel in fresh contexts | `research` tool calls in one Lead reply run concurrently; each Researcher is a new `Runner.run` (`spawn_researcher`) |
| Lead goes back for more when findings leave a gap, up to a cap | Gap review loop in `research_run`; at most 3 rounds and 8 Researchers, enforced in code |
| Vague questions require clarification | Clarifier (`status: clarify`); the test set's C1 "Is Smith for real?" |
| Output is accurately cited | `[Q#]` citations; `agent/verify.py` checks every number against the cited rows, and the Critic checks meaning |
| A data source per team member | Baseball Savant (Statcast), Baseball Reference, MLB Stats API (IL and transactions), Chadwick register |
| Creativity | A scouting-report front end built from scratch: live research floor, citation chips, pitcher dashboard with charts, history. Plus the chance check (z scores), a Critic and the code citation check |

## The agent: Pitch Lab

A deep research app over this database. Ask why a pitcher got better or worse; five kinds of agent
work on it, and the page shows every step live. The models are Gemini on Vertex AI: Gemini 3.6 Flash
for the agents below, and Flash-Lite for a quick off-topic check.

| Agent | Job | Sees |
|---|---|---|
| **Clarifier** | Decides if the question is specific enough. Resolves names to MLBAM ids, and works out the two periods to compare. Asks the user when a pitcher is missing or ambiguous ("Is Smith for real?") or the question has no angle ("Skenes?"). If only part of a question can be answered, it goes ahead and records what can't. A guardrail stops off-topic messages before it runs. | the question, `find_pitcher` |
| **Lead** | Splits the question into 2-4 angles (stuff, results vs. luck, health & workload, history, league scan) and calls `research` for all of them in one turn, so the Researchers run in parallel. After each round, it reviews the gaps the Researchers reported and sends a follow-up round for the ones the data can answer. | the question, the chance check, season lines, findings |
| **Researcher** ×N | One task, one focus, a fresh context. Queries the database with shaped tools (`pitch_arsenal`, `injury_history`, `compare_periods`, `check_change`, `league_context`, ...) or read-only SQL. Every result has a citation id like `[Q7]`. | its task, the schema doc |
| **Writer** | Writes the report from the findings only: verdict (skill / luck), confidence, whether an injury overlaps, sections, limits, next questions. Every number is cited. | question, Lead's brief, findings, the chance check |
| **Critic** | Judges whether each claim is supported by the evidence: accuracy, causes, stat definitions and the verdict. Its feedback goes back to the Writer, up to twice. | the report and the rows it cites |

Caps are enforced in code, not just the prompts: at most 8 researchers, 3 rounds, 8 successful
queries per researcher, 2 revisions of the report, and a time budget (no follow-up round after 150 s,
research stops at 200 s, no new revision after 300 s).

**Speed.** A run usually takes 1.5 to 3 minutes; Gavin Williams took 158 s before the changes below and
94 s after (one run each, and the second needed one research round instead of two, so not all of it is
the changes). The model stays Gemini 3.6 Flash: the test set showed that Flash-Lite researchers fail more
queries and get numbers wrong (`evals/baseline.md`). Instead:
- The Writer's report streams to the page as it's written, so the reader starts reading while the
  rest of the run finishes. It's marked as a draft until the checks finish.
- The Lead's gap reviews run at low reasoning effort. Picking which listed gaps the data can fill
  doesn't need the deeper thinking its first plan gets.
- Each model call has a timeout of a few times its usual length (45 s for a Researcher, 90 s for the
  Lead, 120 s for the Writer and Critic) and one retry, so one stalled call can't hold up a round for minutes.
- The run record has each agent's model time (`tokens_by_agent[...]["seconds"]`), to show where a
  slow run spent its time.

**Is a change real?** `check_change` compares two periods across 12 stats and gives both values, the
sample sizes, and a z score: the change measured against normal noise. Code runs it for each pitcher on
the periods the question asks about, so the Lead and Writer always have it. The tool gives numbers,
not labels, and the agents judge them. Verdicts say what changed, not why: an injury is reported
separately as a timing overlap, and a possible cause stays a hypothesis unless a finding shows the mechanism.

**Off-topic and unanswerable questions.** A blocking guardrail stops messages that have nothing to do
with pitchers. If nothing in a question can be answered, like one that's only about Stuff+, the Clarifier
says so and suggests related questions. If you pick one, the report still says what it can't answer.

**When a researcher's SQL fails** (a column missing from GROUP BY, a column that lives in another
table), it gets DuckDB's error plus a hint (`agent/evidence.py: explain_error`) and tries again. Failed
queries get no citation id and don't use up the 8-query budget; hand-written SQL has its own cap of 4
failures per researcher, and the shaped tools keep working past it. The page shows these as
"⚠ SQL error" (normal, retried).

**When a researcher runs out of turns** (12 model calls), the queries it ran are kept: one last call,
with its tools removed, writes the findings from the results already in its context ("wrapped up at
its turn limit"). Only if that fails too does the page show "✕ didn't finish"; that task then shows up
as a gap for the Lead.

**When an agent fails**, for example on a Gemini rate limit, it gets one retry and the run carries on with
what it has, so a failure can't throw away finished work. If the Writer fails, you get the Researchers'
findings, labelled as unedited. A run only stops with an error if no Researcher finished.

**Citations are checked by code** (`agent/verify.py`). Every query is stored with its exact SQL and
rows. After the Writer finishes, every number in every sentence is looked up in the rows of the
queries that sentence cites: as is, as a percentage of a 0-1 rate, or as the change between two
cited values, at the precision the sentence uses. A number can also be part of the query itself, like
the filter in "at least 150 batters faced". "Next questions" isn't checked, because those are questions,
not claims. The page shows the score ("63/65 numbers match their
cited queries"), and each citation opens the rows and a link to the upstream source (Baseball
Savant, Baseball Reference, MLB.com).

**The Critic checks meaning** (`agent/review.py`), which a number check can't. Problems from either check
go back to the Writer up to twice (`MAX_REVISIONS`): edits first, then a full rewrite. The version with
the fewest problems is kept. Claims still unresolved are listed in the report as "couldn't be confirmed",
and its confidence is capped at Medium. Notes on wording go to the Writer only. The Critic is a model,
so it's a safety net and not a guarantee: it catches some problems each run, and which ones varies.

The database is opened read-only with file and network access off, so model-written SQL can't
change data or read files.

### Run it locally (after cloning)

You need: [uv](https://docs.astral.sh/uv/), the [gcloud CLI](https://cloud.google.com/sdk/docs/install),
a Google Cloud project with the Vertex AI API enabled (for Gemini), and read access to the
`mlb-agent-2026` bucket (for the database; ask its owner).

```bash
git clone https://github.com/zenotheboi/mlb-pitcher-agent.git
cd mlb-pitcher-agent
```
```bash
gcloud auth application-default login   # one login for Gemini (Vertex AI) and the data bucket
cp .env.example .env                    # then set VERTEXAI_PROJECT=<your project id> in .env
uv sync
uv run app.py                           # first start downloads data/baseball.duckdb (164 MB)
```

Open http://localhost:8000. If you already have `data/baseball.duckdb` (e.g. from `gsutil cp`), it
is used as is and nothing is downloaded.

Terminal only: `uv run python -m agent.cli "Why did Paul Skenes give up more home runs in 2026?"`.
Tests: `uv run pytest`.
Test set: `uv run python -m evals.run score` scores saved runs for free, and
`uv run python -m evals.run run C1 C2 C3` runs questions, which spends credits. `evals/baseline.md` has the
results so far. A full question costs about 30 cents at the time of writing.

Logs: every run, researcher, failed query (with its SQL and the hint sent back), unverified number
and error goes to `logs/pitchlab.log` (and stderr, which Cloud Run sends to Cloud Logging). Each line
carries the run id, so `grep 41f835fd logs/pitchlab.log` shows one run.

Run records: each run also saves one JSON file in `logs/runs/`, however it ends: the settings, findings,
report, every query with its rows, the full SQL of failed queries, tokens by agent, and each review
step. Set `RUNS_DIR` to save
elsewhere, or leave it empty to turn this off. It's off by default on Cloud Run.

### Layout

```
app.py              FastAPI: /run streams a research run as Server-Sent Events; POST /chat returns it as JSON
                    with every tool call; /api/pitcher, /api/search
agent/pipeline.py   the agents, rounds, gap review, review loop, retries, caps, time budget
agent/tools.py      the agents' database tools
agent/evidence.py   read-only SQL; every query becomes a citable Evidence record
agent/verify.py     the citation check
agent/review.py     the Critic's output, and how unresolved problems are shown
agent/prompts.py    system prompts (Researchers get docs/schema.md)
agent/logs.py       logging: logs/pitchlab.log + stderr, tagged with the run id
agent/runs.py       one JSON record per run in logs/runs/
evals/              the test set: fixed questions with known answers, and results so far
docs/samples/       the README's sample queries: /chat replies, a run record, and the no-tools answers
static/             the page: index.html, app.js, charts.js, styles.css
context_panel.py, context-panel.js   the course template's context panel ("Agent contexts" button)
```

### Deploy to Cloud Run

```bash
gcloud run deploy pitch-lab --source . --region us-east1 --memory 2Gi --cpu 2 \
  --timeout 600 --min-instances 1 --max-instances 1 \
  --set-env-vars VERTEXAI_PROJECT=<project>,VERTEX_LOCATION=global
```

- `--source .` builds the `Dockerfile` with Cloud Build: the project needs the Cloud Run, Cloud Build
  and Artifact Registry APIs enabled.
- The service account needs `roles/aiplatform.user` in `<project>` and Storage Object Viewer on the
  `mlb-agent-2026` bucket. At startup it downloads the database (the image doesn't contain it).
- `--max-instances 1`: runs, `/chat` sessions and the context panel are kept in memory, so every request
  of a run must reach the same instance. `--timeout 600` covers a run's event stream and a `/chat` call;
  a run usually takes 1.5 to 3 minutes.
- After deploying, put the service URL in `submission.json` and at the top of this README.
- `/run` and `/chat` have no login, rate limit or token cap, and each question spends Vertex credits.
  Keep the URL to the graders, or add those before sharing it widely.

## Get the database

The current data is `v3`, which fixes injured list stints that were wrongly merged. **If you downloaded
the database before `v3`, update it:** the app reuses any `data/baseball.duckdb` it finds, so pulling the
code doesn't update your database. Delete it, and the next start downloads `v3`:

```bash
rm data/baseball.duckdb
uv run app.py
```

```bash
gcloud auth login
```
```bash
gsutil cp gs://mlb-agent-2026/baseball-agent/v3/baseball.duckdb data/
```

```python
import duckdb
con = duckdb.connect("data/baseball.duckdb", read_only=True)
con.sql("SELECT * FROM pitcher_season WHERE season = 2026 LIMIT 5").show()
```

In the app, call `ensure_db()` at startup instead. It downloads the file from GCS if it is
missing:

```python
from data_pipeline.db import ensure_db, connect_readonly
ensure_db()
con = connect_readonly()
```

Cloud Run: deploy in `us-east1` (same region as the bucket), 2 GiB memory, and give the service
account Storage Object Viewer on `mlb-agent-2026`.

## What data there is

| Table | One row per | Seasons | Source |
|---|---|---|---|
| `pitches` | pitch (2.14M rows) | 2024-2026 | Baseball Savant (Statcast) |
| `pitcher_season` | pitcher-season | 2024-2026 | from `pitches` |
| `pitcher_season_pitch_type` | pitcher-season-pitch type | 2024-2026 | from `pitches` |
| `pitcher_month` | pitcher-season-month | 2024-2026 | from `pitches` |
| `pitcher_game` | pitcher-game | 2024-2026 | from `pitches` |
| `bref_pitching_standard` | pitcher-season (ERA, FIP, IP, SO...) | 2015-2026 | Baseball Reference |
| `bref_pitching_value` | pitcher-season (WAR) | 2015-2026 | Baseball Reference |
| `il_stints` | injured list stint | 2024-2026 | MLB Stats API |
| `transactions` | transaction | 2024-2026 | MLB Stats API |
| `mlb_people` | pitcher (birth date, hand, debut) | - | MLB Stats API |
| `players` | player (ID crosswalk) | - | Chadwick register |
| `data_dictionary` | column (meaning, unit) | - | - |

Regular season only. All player IDs are MLBAM IDs (`pitches.pitcher`, `mlbam_id`).
Every column and metric definition is in [`docs/schema.md`](docs/schema.md).

## Rebuild the data (only if it needs to change)

```bash
uv sync
```
```bash
uv run python -m data_pipeline.fetch_chadwick
```
```bash
uv run python -m data_pipeline.fetch_statcast --seasons 2024 2025 2026
```
```bash
uv run python -m data_pipeline.fetch_bref --seasons 2015-2026
```
```bash
uv run python -m data_pipeline.fetch_mlb_api --seasons 2024 2025 2026
```
```bash
uv run python -m data_pipeline.build_db
```
```bash
uv run python -m data_pipeline.validate
```
```bash
uv run python -m data_pipeline.upload_gcs --version v4
```

Baseball Reference blocks scripts, so its pages are saved by hand in a browser and kept in
`data/raw/bref/manual/`. Upload new data as a new version (`v4`); never overwrite an existing one.
