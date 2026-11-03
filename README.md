# mlb-pitcher-agent

Deep Research agent: *Why do MLB pitchers get better or worse? Separating real skill changes
from luck and injury.*

## The agent: Pitch Lab

A deep research app over this database. Ask why a pitcher got better or worse; four agent shapes
work on it, and the page shows every step live.

| Agent | Job | Sees |
|---|---|---|
| **Clarifier** | Decides if the question is specific enough. Resolves names to MLBAM ids; asks the user when a pitcher is missing or ambiguous ("Is Smith for real?"), the question has no angle ("Skenes?"), or it needs data we don't have (Stuff+). | the question, `find_pitcher` |
| **Lead** | Splits the question into 2-4 angles (stuff, results vs. luck, health & workload, history, league scan) and calls `research` for all of them in one turn, so the Researchers run in parallel. After each round, it reviews the gaps the Researchers reported and sends a follow-up round for the ones the data can answer. | the question, season lines, findings |
| **Researcher** ×N | One task, one focus, a fresh context. Queries the database with shaped tools (`pitch_arsenal`, `injury_history`, `compare_periods`, `league_context`, ...) or read-only SQL. Every result has a citation id like `[Q7]`. | its task, the schema doc |
| **Writer** | Writes the report from the findings only: verdict (skill / luck / injury), sections, limits, next questions. Every number is cited. | question, Lead's brief, findings |

Caps are enforced in code, not just the prompts: at most 8 researchers, 3 rounds, 8 successful
queries per researcher, and a time budget (no follow-up round after 150 s, research stops at 200 s),
so a run finishes in about 2 minutes.

**When a researcher's SQL fails** (a column missing from GROUP BY, a column that lives in another
table), it gets DuckDB's error plus a hint (`agent/evidence.py: explain_error`) and tries again. Failed
queries get no citation id and don't use up the 8-query budget; they have their own cap of 4 per
researcher. The page shows these as "⚠ SQL error" (normal, retried).

**When a researcher runs out of turns** (12 model calls), the queries it ran are kept: one last call,
with its tools removed, writes the findings from the results already in its context ("wrapped up at
its turn limit"). Only if that fails too, or Gemini errors, does the page show "✕ didn't finish"; that
task then shows up as a gap for the Lead.

**Citations are checked by code** (`agent/verify.py`). Every query is stored with its exact SQL and
rows. After the Writer finishes, every number in every sentence is looked up in the rows of the
queries that sentence cites: as is, as a percentage of a 0-1 rate, or as the change between two
cited values, at the precision the sentence uses. Mismatches go back to the Writer once as
find/replace fixes. Whatever still doesn't match is highlighted in the report, and the page shows the
score ("63/65 numbers match their cited queries"). Each citation opens the SQL, the rows, and a link
to the upstream source (Baseball Savant, Baseball Reference, MLB.com).

The database is opened read-only with file and network access off, so model-written SQL can't
change data or read files.

### Run it locally (after cloning)

You need: [uv](https://docs.astral.sh/uv/), the [gcloud CLI](https://cloud.google.com/sdk/docs/install),
a Google Cloud project with the Vertex AI API enabled (for Gemini), and read access to the
`mlb-agent-2026` bucket (for the database; ask its owner).

```bash
git clone https://github.com/zenotheboi/mlb-pitcher-agent.git
cd mlb-pitcher-agent
git checkout joshua_branch
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

Logs: every run, researcher, failed query (with its SQL and the hint sent back), unverified number
and error goes to `logs/pitchlab.log` (and stderr, which Cloud Run sends to Cloud Logging). Each line
carries the run id, so `grep 41f835fd logs/pitchlab.log` shows one run.

### Layout

```
app.py              FastAPI: /run streams a research run as Server-Sent Events; /api/pitcher, /api/search
agent/pipeline.py   Clarifier, Lead, Researcher, Writer; rounds, gap review, caps, time budget
agent/tools.py      the agents' database tools
agent/evidence.py   read-only SQL; every query becomes a citable Evidence record
agent/verify.py     the citation check
agent/logs.py       logging: logs/pitchlab.log + stderr, tagged with the run id
agent/prompts.py    system prompts (Researchers get docs/schema.md)
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
  reach the same instance. `--timeout 600` covers a run's event stream; a run takes about 2 minutes.
- Not yet deployed or tested on Cloud Run.

## Get the database

```bash
gcloud auth login
```
```bash
gsutil cp gs://mlb-agent-2026/baseball-agent/v2/baseball.duckdb data/
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
uv run python -m data_pipeline.upload_gcs --version v3
```

Baseball Reference blocks scripts, so its pages are saved by hand in a browser and kept in
`data/raw/bref/manual/`. Upload new data as a new version (`v3`); never overwrite `v2`.
