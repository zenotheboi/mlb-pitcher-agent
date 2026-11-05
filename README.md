# mlb-pitcher-agent

Deep Research agent: *Why do MLB pitchers get better or worse? Separating real skill changes
from luck and injury.*

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
research stops at 200 s, no new revision after 300 s). A run usually takes 2 to 5 minutes.

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
cited queries"), and each citation opens the SQL, the rows, and a link to the upstream source (Baseball
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
app.py              FastAPI: /run streams a research run as Server-Sent Events; /api/pitcher, /api/search
agent/pipeline.py   the agents, rounds, gap review, review loop, retries, caps, time budget
agent/tools.py      the agents' database tools
agent/evidence.py   read-only SQL; every query becomes a citable Evidence record
agent/verify.py     the citation check
agent/review.py     the Critic's output, and how unresolved problems are shown
agent/prompts.py    system prompts (Researchers get docs/schema.md)
agent/logs.py       logging: logs/pitchlab.log + stderr, tagged with the run id
agent/runs.py       one JSON record per run in logs/runs/
evals/              the test set: fixed questions with known answers, and results so far
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
- `--max-instances 1`: runs and the context panel are kept in memory, so every request of a run must
  reach the same instance. `--timeout 600` covers a run's event stream; a run usually takes 2 to 5 minutes.
- Not yet deployed or tested on Cloud Run. Before making it public, add login, rate limits and a token
  cap: `/run` has none of these, and each question spends Vertex credits.

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
