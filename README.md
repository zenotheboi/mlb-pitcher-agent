# mlb-pitcher-agent

Deep Research agent: *Why do MLB pitchers get better or worse? Separating real skill changes
from luck and injury.*

This README covers the data pipeline. See `docs/schema.md` for every table and column.

## Where the data came from and how to load it

### Just use the data (teammates, Cloud Run)

```bash
gsutil cp gs://mlb-agent-2026/baseball-agent/v1/baseball.duckdb data/
```

```python
import duckdb
con = duckdb.connect("data/baseball.duckdb", read_only=True)
```

In the app, call `data_pipeline.db.ensure_db()` at startup. It uses `data/baseball.duckdb` if it
exists, else builds it from `data/processed/*.parquet`, else downloads from
`gs://mlb-agent-2026/baseball-agent/v1/` (env vars `GCS_BUCKET`, `GCS_DATA_VERSION`).

### Sources

| Source | Table(s) | Seasons | How |
|---|---|---|---|
| Baseball Savant (Statcast) | `pitches` | 2024-2026 regular season | Savant search CSV endpoint, 3-day chunks |
| Baseball Reference | `bref_pitching_standard`, `bref_pitching_value` | 2015-2026 | League pages, saved by hand (see below) |
| MLB Stats API | `transactions`, `il_stints`, `mlb_people` | 2024-2026 | `/api/v1/transactions`, `/api/v1/people` |
| Chadwick Bureau register | `players` | all | GitHub `chadwickbureau/register` |

Every download is logged (URL, time, row count) in `data/raw/fetch_log.csv`
(uploaded to GCS as `raw/fetch_log.csv`).

### Rebuild from scratch

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
uv run python -m data_pipeline.fetch_bref --seasons 2015-2026 --no-download
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
uv run python -m data_pipeline.upload_gcs --version v1
```

Raw downloads are cached in `data/raw/` (gitignored) and never re-downloaded; add `--force` to
re-download. Run `fetch_mlb_api` after `fetch_statcast` (it needs the pitcher ids for
`mlb_people`).

### Baseball Reference: saved by hand

Baseball Reference has no API and blocks automated clients, so the 24 league pages are saved by
a person in a normal browser. For each link below, either

- save the whole page (Ctrl+S / Cmd+S, "Webpage, HTML only") as `{season}_{page}.html`, or
- use *Share & Export → Get table as CSV* on the player table and save it as `{season}_{page}.csv`

and put the files in `data/raw/bref/manual/`. Then run `fetch_bref --no-download`.

| Season | standard | value |
|---|---|---|
| 2015 | [2015_standard](https://www.baseball-reference.com/leagues/majors/2015-standard-pitching.shtml) | [2015_value](https://www.baseball-reference.com/leagues/majors/2015-value-pitching.shtml) |
| 2016 | [2016_standard](https://www.baseball-reference.com/leagues/majors/2016-standard-pitching.shtml) | [2016_value](https://www.baseball-reference.com/leagues/majors/2016-value-pitching.shtml) |
| 2017 | [2017_standard](https://www.baseball-reference.com/leagues/majors/2017-standard-pitching.shtml) | [2017_value](https://www.baseball-reference.com/leagues/majors/2017-value-pitching.shtml) |
| 2018 | [2018_standard](https://www.baseball-reference.com/leagues/majors/2018-standard-pitching.shtml) | [2018_value](https://www.baseball-reference.com/leagues/majors/2018-value-pitching.shtml) |
| 2019 | [2019_standard](https://www.baseball-reference.com/leagues/majors/2019-standard-pitching.shtml) | [2019_value](https://www.baseball-reference.com/leagues/majors/2019-value-pitching.shtml) |
| 2020 | [2020_standard](https://www.baseball-reference.com/leagues/majors/2020-standard-pitching.shtml) | [2020_value](https://www.baseball-reference.com/leagues/majors/2020-value-pitching.shtml) |
| 2021 | [2021_standard](https://www.baseball-reference.com/leagues/majors/2021-standard-pitching.shtml) | [2021_value](https://www.baseball-reference.com/leagues/majors/2021-value-pitching.shtml) |
| 2022 | [2022_standard](https://www.baseball-reference.com/leagues/majors/2022-standard-pitching.shtml) | [2022_value](https://www.baseball-reference.com/leagues/majors/2022-value-pitching.shtml) |
| 2023 | [2023_standard](https://www.baseball-reference.com/leagues/majors/2023-standard-pitching.shtml) | [2023_value](https://www.baseball-reference.com/leagues/majors/2023-value-pitching.shtml) |
| 2024 | [2024_standard](https://www.baseball-reference.com/leagues/majors/2024-standard-pitching.shtml) | [2024_value](https://www.baseball-reference.com/leagues/majors/2024-value-pitching.shtml) |
| 2025 | [2025_standard](https://www.baseball-reference.com/leagues/majors/2025-standard-pitching.shtml) | [2025_value](https://www.baseball-reference.com/leagues/majors/2025-value-pitching.shtml) |
| 2026 | already saved | already saved |

A saved HTML page is better than the CSV: it keeps the per-team rows of traded pitchers and the
player id in every row. (Fill in the CSV header names in `CSV_MAP` in `fetch_bref.py` if
B-Ref changes them.)

### Data size

Measured after the full build: _(filled in after the build)_.

## Layout

```
data_pipeline/   fetch_*.py, build_db.py, validate.py, upload_gcs.py, db.py (ensure_db)
data/raw/        raw downloads + fetch_log.csv (gitignored)
data/processed/  cleaned Parquet
data/baseball.duckdb
docs/schema.md   generated schema for the agent prompts
```
