# Pitch Lab (mlb-pitcher-agent)

A Deep Research agent for one question: *why did an MLB pitcher get better or worse, and is the change
real skill, luck, or injury?* It answers from the 2024–2026 pitch-by-pitch data, Baseball Reference
season lines and the injured list, and every number in its report links to the query that produced it.

**Who it's for:** fantasy managers, bettors, analysts and fans who see a pitcher's ERA jump or drop
and want to know whether it will last. ERA moves with luck; strikeouts, whiffs, expected stats and
velocity show whether the pitcher himself changed. Pitch Lab checks both and measures each change
against normal noise.

**Data:** Baseball Savant's Statcast pitch-level data (2.14M regular-season pitches, 2024–2026),
Baseball Reference season lines (2015–2026, saved by hand because the site blocks scripts), the MLB
Stats API's injured list and transactions (2024–2026), and the Chadwick register to join their player
IDs. Our scripts in `data_pipeline/` fetch them and build one read-only DuckDB file, kept in a GCS
bucket that the app downloads on first start. See [Get the database](docs/DESIGN.md#get-the-database) and
[What data there is](docs/DESIGN.md#what-data-there-is).

**Live app:** https://mlb-pitcher-agent-75067425867.us-east1.run.app (sign in with a Columbia Google account; also in [`submission.json`](submission.json))
**Path:** Deep Research (Lead, parallel Researchers, Writer, plus a Clarifier and a Critic)

## Agents

| Agent | Job |
|---|---|
| **Clarifier** | Checks the question is specific: resolves pitcher names, picks the periods to compare, and asks back when it's vague. Says what the data can't answer. |
| **Lead** | Splits the question into 2–4 angles and starts that many Researchers in one turn. After each round, sends follow-up Researchers for the gaps they report (at most 3 rounds, 8 Researchers). |
| **Researcher** ×N | One task in a fresh context. Queries the database with 12 read-only tools; every result gets a citation id like `[Q7]`. |
| **Writer** | Writes the report from the findings: verdict (skill or luck), confidence, injury overlap, limits. Every number is cited. |
| **Critic** | Checks each claim against the rows it cites, and sends problems back to the Writer, up to twice. Code also checks every number. |

All on Gemini 3.6 Flash (Vertex AI). [How the agents work](docs/DESIGN.md#how-the-agents-work) has the details.

## How to use

Type a question that names a pitcher and what changed, or click an example. If it's vague ("Is Smith
for real?"), the agent asks which pitcher or angle first. A run usually takes 1.5 to 3 minutes: the page
shows the agents working live, and the report appears as it's written. Click any `[Q#]` citation to
see the rows behind that number and a link to the upstream source.

To go deeper, ask a follow-up under the report ("Was it his slider?") or click a suggested next question.
The agents read the earlier reports, research only what's new, and can cite the earlier evidence. The box at
the top starts a new conversation.

The same research is available as JSON from [`POST /chat`](docs/DESIGN.md#post-chat), in Project 1's
shape (`response`, `session_id`, `tool_calls`, with an `agent` field on every tool call).

**What it can't answer**, and says so when asked: Stuff+, Location+, xFIP or SIERA (FanGraphs models),
medical details, mechanics or coaching notes, the minors, spring training and the postseason.
Pitch-level data starts in 2024.

## Sample queries

Run on 2026-11-05. "No tools" is the same model the agents use, `gemini-3.6-flash` on Vertex AI, given
only the question: no tools and no system prompt. Full outputs are in
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

## Assignment checklist

| Requirement | Where |
|---|---|
| Data the model doesn't know deeply | The 2026 season, pitch by pitch. With no tools, the model says 2026 hasn't happened (samples above) |
| Gets at the data through tools | Read-only SQL over DuckDB: 11 shaped tools plus `run_sql` (`agent/tools.py`) |
| Combines evidence into a supported claim | A verdict (skill / luck / injury overlap) from the chance check's z scores and the findings |
| Says when the data can't answer | Clarifier `out_of_scope` (sample 3), "What the data can't tell us" in every report |
| Shows its work; `/chat` keeps `response`, `session_id`, `tool_calls`, sub-agents' calls carry `agent` | [`POST /chat`](docs/DESIGN.md#post-chat); the page's research floor, evidence panel and "Agent contexts" |
| Front end says what it explores, who it's for, how to use it | The top of the page: For / How / Can't |
| Deployed on Cloud Run | [Deploy to Cloud Run](docs/DESIGN.md#deploy-to-cloud-run); URL in `submission.json` |
| At least three agent shapes | Clarifier, Lead, Researcher, Writer, Critic |
| Lead fans out to Researchers in one turn, run in parallel in fresh contexts | `research` tool calls in one Lead reply run concurrently; each Researcher is a new `Runner.run` (`spawn_researcher`) |
| Lead goes back for more when findings leave a gap, up to a cap | Gap review loop in `research_run`; at most 3 rounds and 8 Researchers, enforced in code |
| Vague questions require clarification | Clarifier (`status: clarify`); the test set's C1 "Is Smith for real?" |
| Output is accurately cited | `[Q#]` citations; `agent/verify.py` checks every number against the cited rows, and the Critic checks meaning |
| A data source per team member | Baseball Savant (Statcast), Baseball Reference, MLB Stats API (IL and transactions), Chadwick register |
| Creativity | A scouting-report front end built from scratch: live research floor, citation chips, pitcher dashboard with charts, history. Plus the chance check (z scores), a Critic and the code citation check |

## Run locally

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

Tests: `uv run pytest` (no model calls).

## More

[`docs/DESIGN.md`](docs/DESIGN.md) has the full details: the `/chat` API, how each agent works and
what it sees, caps and speed, failure handling, the citation check and Critic, logs and run records,
the project layout, deploying to Cloud Run, the database and how to rebuild it.
[`docs/schema.md`](docs/schema.md) describes every table and column, and
[`evals/baseline.md`](evals/baseline.md) has the test set results.
