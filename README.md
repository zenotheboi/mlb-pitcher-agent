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
with no tools and no system prompt. Both answers are quoted from runs on 2026-11-05 (trimmed with "…");
the full outputs are in [`docs/samples/`](docs/samples/).

### 1. "Why did Gavin Williams get worse in 2026? His ERA rose from 3.06 to 3.76."

**Expected:** the premise is wrong: he pitched better, and the ERA rise is luck on balls in play.

| Fact | 2025 → 2026 | Source |
|---|---|---|
| ERA / FIP | 3.06 → 3.76 / 4.39 → 3.48 | `bref_pitching_standard` |
| K% / BB% | 24.6% → 33.2% / 11.8% → 8.2% | `pitcher_season` (Statcast) |
| xwOBA / BABIP | .323 → .301 / .254 → .288 | `pitcher_season` (Statcast) |
| IL time | none | `il_stints` (MLB Stats API) |

| No tools | Pitch Lab |
|---|---|
| "As the 2026 MLB season is either in progress, projected, or part of a baseball simulation… Here are the primary reasons why Gavin Williams' ERA would regress… his underlying metrics (FIP, xFIP, and SIERA) usually hang around the **3.80–4.20 range**." | "**Verdict: Mixed: skill + luck** · Confidence: High · Injury overlap: No. Gavin Williams did not actually pitch worse in 2026… his FIP dropped from 4.39 to 3.48, his strikeout rate jumped from 24.6% to 33.2% (z = 3.7), and his walk rate dropped from 11.8% to 8.2%… his expected outcomes improved to a .301 xwOBA, but his BABIP normalized to .288." |

No tools accepts the false premise and invents numbers. Pitch Lab corrects it with the real 2026 numbers
(80/80 match their cited queries). Its label should be "Real skill gain", though the text says so.
[Full JSON](docs/samples/chat_gavin_williams.json)

### 2. "Why did Drew Rasmussen improve in 2026?"

**Expected:** a real skill gain with moderate evidence (Medium confidence: K% z = 1.8), alongside a new changeup.

| Fact | 2025 → 2026 | Source |
|---|---|---|
| ERA / FIP | 2.76 → 2.67 / 3.84 → 2.82 | `bref_pitching_standard` |
| K% / chase% / xwOBA | 21.7% → 26.0% / 28.7% → 33.1% / .291 → .271 | `pitcher_season` (Statcast) |
| Changeups thrown | 28 → 354 (four-seamer unchanged at 95.8 mph) | `pitcher_season_pitch_type` (Statcast) |
| IL time | none in 2025–26 | `il_stints` (MLB Stats API) |

| No tools | Pitch Lab |
|---|---|
| "…(since 2026 is either in the future or currently unfolding…) several key factors explain why Rasmussen is primed for a major surge or breakthrough by 2026: Distance from 2023 Elbow Surgery… Pitch Repertoire Optimization by the Rays… Refining the Sweeper/Cutter." | "**Verdict: Real skill gain** · Confidence: High · Injury overlap: No. …maintaining identical main fastball velocity (95.8 mph in both 2025 and 2026, z = 0.4), Rasmussen raised his strikeout rate from 21.7% to 26.0% (z = 1.8)… the resurgence of Rasmussen's changeup, which jumped from 1.2% usage (28 pitches) in 2025 to 13.3% usage (354 pitches) in 2026." |

No tools has no 2026 data and guesses at a sweeper. Pitch Lab finds the changeup and the real numbers
(90/90 match). Two misses: Confidence should be Medium, and it says the changeup "drove" the gain, which
the data only shows happening at the same time. [Full JSON](docs/samples/chat_drew_rasmussen.json)

### 3. "What was Tarik Skubal's Stuff+ in 2026?"

**Expected:** no value. Stuff+ is a FanGraphs model and isn't in the database, so the agent should say so
and offer what it can measure. **Source:** none; [`docs/schema.md`](docs/schema.md) lists every column.

| No tools | Pitch Lab |
|---|---|
| "The 2026 MLB season has not happened yet… during his 2024 American League Cy Young winning season, Tarik Skubal posted an overall **Stuff+ of 112**." | "FanGraphs Stuff+ is not available in our dataset, but we can analyze Tarik Skubal's pitch quality using Statcast metrics like velocity, movement, whiff rate, and expected xwOBA." Then offers: "Compare Skubal's 2026 pitch metrics (velocity, movement, whiff%, xwOBA) to 2025." |

No tools gives a number that can't be checked. Pitch Lab says it doesn't have the data and runs no research.
[Full JSON](docs/samples/chat_skubal_stuff_plus.json)

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
