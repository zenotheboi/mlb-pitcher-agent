# Pitch Lab (mlb-pitcher-agent)

A Deep Research agent that answers one question: **why did an MLB pitcher get better or worse, and
is the change real skill, luck, or injury?** Every number in its report links to the query behind it.

- **Live app:** https://mlb-pitcher-agent-75067425867.us-east1.run.app (sign in with a Columbia Google account)
- **Path:** Deep Research (Clarifier, Lead, parallel Researchers, Writer, Critic)
- **Who it's for:** fantasy managers, bettors, analysts and fans who see a pitcher's ERA jump or drop
  and want to know if it will last. ERA moves with luck; strikeouts, whiffs, expected stats and velocity
  show whether the pitcher himself changed. Pitch Lab checks both and measures each change against
  normal noise.

## Data

| Source | What | Seasons | Tables |
|---|---|---|---|
| Baseball Savant (Statcast) | 2.14M regular-season pitches | 2024–2026 | `pitches` and per-season, pitch-type, month and game summaries |
| Baseball Reference | Season lines: ERA, FIP, IP, WAR (saved by hand; the site blocks scripts) | 2015–2026 | `bref_pitching_standard`, `bref_pitching_value` |
| MLB Stats API | Injured list stints and transactions | 2024–2026 | `il_stints`, `transactions`, `mlb_people` |
| Chadwick register | Player ID crosswalk between the sources | – | `players` |

Our scripts in `data_pipeline/` fetch these and build one read-only DuckDB file (164 MB), stored in a
GCS bucket. The app downloads it on first start. To load it yourself:

```bash
gsutil cp gs://mlb-agent-2026/baseball-agent/v3/baseball.duckdb data/
```

Every column is described in [`docs/schema.md`](docs/schema.md); how the data was built is in
[`docs/DESIGN.md`](docs/DESIGN.md#get-the-database).

## Sample queries

"No tools" is the same model the agents use (`gemini-3.6-flash` on Vertex AI) given only the question,
with no tools and no system prompt. Run on 2026-11-05; full outputs are in [`docs/samples/`](docs/samples/).

### 1. "Why did Gavin Williams get worse in 2026? His ERA rose from 3.06 to 3.76."

**Expected:** the premise is wrong. ERA rose, but he pitched better: FIP 4.39 → 3.48, K% 24.6% → 33.2%,
BB% 11.8% → 8.2%, xwOBA .323 → .301. The ERA rise came from BABIP (.254 → .288). No IL time.
**Source:** `pitcher_season`, `bref_pitching_standard`, `il_stints`.

| No tools | Pitch Lab |
|---|---|
| Says the 2026 season may not have happened. Accepts the premise and explains a decline from his general profile (walks, fastball reliance), with invented numbers like FIP "around 3.80–4.20". | "Gavin Williams did not actually pitch worse in 2026." Finds the FIP, K% (z = 3.7), BB% and xwOBA gains, and the BABIP rise. **80/80 numbers match their cited queries.** One miss: labels it "Mixed: skill + luck" where we expect "Real skill gain". [JSON](docs/samples/chat_gavin_williams.json) |

### 2. "Why did Drew Rasmussen improve in 2026?"

**Expected:** a skill gain with moderate evidence. ERA barely moved (2.76 → 2.67), but FIP 3.84 → 2.82,
K% 21.7% → 26.0%, chase% 28.7% → 33.1%, xwOBA .291 → .271. His changeup went from 28 pitches to 354;
four-seamer unchanged at 95.8 mph. No IL time. Confidence should be Medium (K% z = 1.8).
**Source:** `pitcher_season`, `pitcher_season_pitch_type`, `bref_pitching_standard`, `il_stints`.

| No tools | Pitch Lab |
|---|---|
| Says 2026 is "in the future or currently unfolding". Has no 2026 numbers; reasons from his surgery history and "The Rays Way", mentioning a changeup only as a general idea. | "Verdict: Real skill gain." Finds the FIP, K%, chase% (z = 2.4) and exit velocity gains, the steady BABIP, and the changeup jump from 1.2% to 13.3% of pitches. **90/90 numbers match.** Two misses: Confidence High instead of Medium, and it says the changeup "drove" the gain, which the data doesn't show. [JSON](docs/samples/chat_drew_rasmussen.json) |

### 3. "What was Tarik Skubal's Stuff+ in 2026?"

**Expected:** the data can't answer it. Stuff+ is a FanGraphs model and isn't in the database, so the
agent should give no value and offer what it can measure.
**Source:** none; [`docs/schema.md`](docs/schema.md) lists every column, and Stuff+ isn't one.

| No tools | Pitch Lab |
|---|---|
| Says 2026 hasn't happened, then gives a 2024 "Stuff+ of 112" that can't be checked. | "FanGraphs Stuff+ is not available in our dataset", and offers two related questions it can answer from Statcast. Runs no research. [JSON](docs/samples/chat_skubal_stuff_plus.json) |

## How to use

- Ask about a named pitcher and what changed, or click an example. Vague questions ("Is Smith for
  real?") get a clarifying question first.
- A run takes 1.5 to 3 minutes. The page shows the agents working live; click any `[Q#]` to see the
  rows behind a number and its upstream source.
- Ask a follow-up under the report ("Was it his slider?") to go deeper. The same research is available
  as JSON from [`POST /chat`](docs/DESIGN.md#post-chat).

**It can't answer:** Stuff+, Location+, xFIP or SIERA (FanGraphs), medical details, mechanics or
coaching, the minors, spring training or the postseason. Pitch-level data starts in 2024.

## Agents

| Agent | Job |
|---|---|
| **Clarifier** | Resolves pitcher names and the periods to compare; asks back when the question is vague or out of scope. |
| **Lead** | Splits the question into 2–4 angles and starts that many Researchers at once; sends follow-ups for gaps (max 3 rounds, 8 Researchers). |
| **Researcher** ×N | One task in a fresh context, using 12 read-only database tools. Every result gets a citation id like `[Q7]`. |
| **Writer** | Writes the report: verdict, confidence, injury overlap, limits. Every number is cited. |
| **Critic** | Checks each claim against its cited rows and sends problems back to the Writer (up to twice). Code also checks every number. |

All run on Gemini 3.6 Flash. Details: [How the agents work](docs/DESIGN.md#how-the-agents-work).

## Assignment checklist

| Requirement | Where |
|---|---|
| Data the model doesn't know | The 2026 season, pitch by pitch; with no tools the model says 2026 hasn't happened |
| Gets data through tools | 11 shaped read-only tools plus `run_sql` over DuckDB (`agent/tools.py`) |
| Combines evidence into a claim | Verdict from the chance check's z scores and the findings |
| Says when data can't answer | Clarifier `out_of_scope` (sample 3); "What the data can't tell us" in every report |
| Shows its work | `/chat` returns `response`, `session_id`, `tool_calls` with an `agent` field; the page shows every step |
| Front end explains itself | Top of the page: For / How / Can't |
| Deployed on Cloud Run | URL above and in `submission.json`; [deploy steps](docs/DESIGN.md#deploy-to-cloud-run) |
| Three or more agent shapes | Clarifier, Lead, Researcher, Writer, Critic |
| Lead fans out in parallel, fresh contexts | `research` calls in one Lead reply run concurrently; each Researcher is a new `Runner.run` |
| Lead fills gaps, with a cap | Gap review loop; max 3 rounds and 8 Researchers, enforced in code |
| Vague questions get clarified | Clarifier `status: clarify` |
| Accurate citations | `[Q#]` ids; `agent/verify.py` checks every number, the Critic checks meaning |
| One data source per team member | Statcast, Baseball Reference, MLB Stats API, Chadwick register |
| Creativity | Custom scouting-report front end (live research floor, citation chips, pitcher charts, history), z-score chance check, Critic |

## Run locally

Needs [uv](https://docs.astral.sh/uv/), the [gcloud CLI](https://cloud.google.com/sdk/docs/install), a
Google Cloud project with Vertex AI enabled, and read access to the `mlb-agent-2026` bucket.

```bash
git clone https://github.com/zenotheboi/mlb-pitcher-agent.git && cd mlb-pitcher-agent
gcloud auth application-default login
cp .env.example .env    # set VERTEXAI_PROJECT=<your project id>
uv sync
uv run app.py           # http://localhost:8000
```

Tests: `uv run pytest` (no model calls). More: [`docs/DESIGN.md`](docs/DESIGN.md) (architecture, `/chat`,
failure handling, deploy, rebuilding the data) and [`evals/baseline.md`](evals/baseline.md) (test results).
