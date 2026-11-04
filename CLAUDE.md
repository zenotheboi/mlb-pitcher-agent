# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Pitch Lab is a multi-agent deep research app that answers why an MLB pitcher got better or worse, separating real skill changes from luck and injury. It runs over a local, read-only DuckDB database.

## Commands

```bash
uv sync                                  # install dependencies (Python 3.11)
uv run app.py                            # web app on http://localhost:8000; downloads the database on first start
uv run python -m agent.cli "<question>"  # one research run in the terminal; add --force to skip clarification

uv run pytest                                                 # all tests
uv run pytest tests/test_verify.py::test_wrong_number_is_flagged  # one test
uv run context_panel.py                                       # the context panel's self-check (not part of pytest)

uv run python -m evals.run score                  # score saved runs against the test set; free
uv run python -m evals.run run C1 C2 C3            # run test questions; spends credits and asks first
```

No linter, formatter or type checker is configured.

Tests that need `data/baseball.duckdb` skip silently when it's missing, so check the skip count. `tests/test_query_budget.py` skips entirely without it. `tests/conftest.py` blocks real model calls, so `pytest` can't spend credits. `tests/test_failures.py` runs the pipeline with fake agents, which covers its control flow but not model behaviour; use `evals/` for that.

Running the agents needs `.env` (copy `.env.example`, set `VERTEXAI_PROJECT`) and `gcloud auth application-default login`. There is no API key: `api_key="unused"` in `agent/pipeline.py` is intentional, because Vertex AI uses Google credentials.

## Work rules

- When developing, use the OpenAI SDK if it offers what you need, rather than writing your own version. In this repo that's the OpenAI Agents SDK (the `agents` package, installed as `openai-agents`).
- Guide the agents instead of listing everything that can go wrong. In prompts, failure handling and guardrails, prefer general principles and one mechanism that covers a class of problems. Treat a specific failure as evidence for a general fix, not as a case to hardcode.
- Vertex AI credits are limited, so be careful with anything that calls a model. A full research run costs about 170,000 to 400,000 input tokens.
  - Check changes with unit tests and the local database first, since those are free.
  - Ask before starting full research runs, or any test that makes repeated model calls.
  - Keep test calls small, and don't repeat a run just to confirm something the run records in `logs/runs/` already show.
  - Run a full test pass only after a large change, such as a model switch or a set of prompt changes.

## Architecture

`agent/pipeline.py:deep_research` runs: Clarifier → Lead → parallel Researchers (up to 3 rounds, with gap review) → Writer → review (code citation check plus Critic, up to `MAX_REVISIONS` rounds of fixes). The `agents` import is the OpenAI Agents SDK, but the models are Gemini on Vertex AI via LiteLLM. Tracing is disabled because it would upload to OpenAI.

Evidence flows through one path, and the citation guarantees depend on it:

1. Every model-visible result goes through `agent/tools.py:_query` → `agent/evidence.py:run_query`, which stores an `Evidence` record in the run's `Ledger` and gives it an id like `Q7`.
2. Researchers cite those ids, and the Writer keeps them.
3. `agent/verify.py` looks up each number in a sentence in the rows of the queries that sentence cites.

A new tool that returns data without going through `_query` produces uncitable numbers. Name lookups (`find_pitcher`) and `describe_table` deliberately bypass it because they aren't citable sources.

`verify.py` checks that a number exists in the cited rows, as a value, a percentage, or a difference. It doesn't check what the number means, so a value attached to the wrong stat or season still passes. Whole numbers of 10 or below are skipped.

The Critic judges what `verify.py` can't: whether claims are supported by the evidence. It's a model, so `agent/review.py` drops problems that aren't really in the report or ledger, and what stays unresolved appears in the report as "couldn't be confirmed", with confidence capped at Medium. The first review uses default effort and re-checks use `low`.

## Coupling that spans files

- **Researcher output format:** `FINDINGS:` / `GAPS:` in `agent/prompts.py` is parsed by regex in `pipeline.py` (`_gap`, `spawn_researcher`, `wrap_up`) and in `static/app.js`.
- **Writer verdict line:** the format in `prompts.WRITER` is parsed by `saveHistory` in `static/app.js`.
- **Citation format:** `[Q7]` / `[Q7, Q9]` is matched by `CITE` in `verify.py` and by `renderInline` in `static/app.js`.
- **Event stream:** events passed to `emit` in `pipeline.py` and `tools.py` are handled by `handle()` in `static/app.js`, and by `show()` in `agent/cli.py`. `done`, `error` and `clarify` end the stream in `app.py`.
- **Limits:** caps are enforced in code (`pipeline.py` constants, `MAX_QUERIES_PER_AGENT` / `MAX_FAILED_PER_AGENT` in `tools.py`). The numbers in the prompts are guidance, not the source of truth. `pitcher_overview` and `injury_history` each use 2 of a researcher's queries.
- **Adding a research tool:** add it to `RESEARCH_TOOLS`, and mention it in `prompts.RESEARCHER` and the relevant `prompts.FOCUS` entry.
- **Adding a table:** update `TABLES` and `SOURCE_OF` in `evidence.py`, `BASE_TABLES` or `SUMMARY_SQL` in `data_pipeline/build_db.py`, `SOURCES` and column descriptions in `data_pipeline/dictionary.py`, and `GRAIN`, `KEYS` and date settings in `data_pipeline/schema_doc.py`.
- **Env vars and import order:** `pipeline.py` reads env vars at import time, so `app.py` and `agent/cli.py` call `load_dotenv` before importing it.

## Data

- `docs/schema.md` is generated by `data_pipeline/build_db.py`, so edit `data_pipeline/dictionary.py` and rebuild instead. It is also loaded into the Researcher system prompt at import, so its size and wording affect every researcher call.
- Metric SQL is defined once as `METRICS` in `build_db.py` and reused by `compare_periods` in `agent/tools.py`.
- Rates are fractions from 0 to 1. `run_value_total` is positive when good for the pitcher, while the raw `delta_run_exp` is from the batting team's view. `avg_velo_fastball` blends FF, SI and FC, so a pitch-mix change can move it without any change in arm speed.
- `prompts.DATA_SCOPE` hardcodes what the data covers, including "as of November 2026". Update it when the data changes.
- `data_pipeline/db.py:ensure_db` uses the local database if present, else builds from `data/processed/*.parquet`, else downloads from `gs://mlb-agent-2026/baseball-agent/v3/`.
- Everything in `data/processed/` is in git except `pitches_*.parquet`, which is large and lives in the bucket.
- The current data is `v3`. `ensure_db` reuses any local `data/baseball.duckdb` without checking its version, so a checkout from before `v3` has a stale database. To update, delete `data/baseball.duckdb` and start the app. Check with `SELECT max(days_on_il) FROM il_stints`: `v2` gives 763, and `v3` gives 538.
- New data is uploaded as a new version (`upload_gcs --version v4`). Never overwrite an existing version: bucket uploads can't be replaced or deleted without the owner. Then update the `GCS_DATA_VERSION` default in `db.py`.
- To rebuild, follow the steps in `README.md` in order, finishing with `data_pipeline.validate`. Baseball Reference blocks scripts, so its pages are saved by hand in `data/raw/bref/manual/`.

## Runtime

- Sessions for the context panel are in memory in one process, so Cloud Run must stay at `--max-instances 1`. The Cloud Run deployment is untested, per the README.
- Reports are stored in the browser's `localStorage`, not on the server.
- Logs go to `logs/pitchlab.log` and stderr, tagged with a run id: `grep <run-id> logs/pitchlab.log`.
- `deep_research` saves a JSON record of every run to `logs/runs/`, however it ends, including settings, findings, every query and token totals. Use these files to compare runs. `RUNS_DIR` overrides the location, and an empty value turns it off.
- `context_panel.py` and `context-panel.js` come from the course template.

## Test set

`evals/questions.json` holds 10 questions with known answers, and `evals/baseline.md` holds the first results. Keep the question wording fixed, so versions stay comparable. `evals/` is outside `tests/` so `uv run pytest` can't spend credits.

The scorer checks outcomes, verdicts and required facts, but it can't tell whether a number is used correctly, and neither can `verify.py`. Read the reports as well: the baseline's wrong numbers all passed the citation check.
