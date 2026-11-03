# mlb-pitcher-agent

Deep Research agent: *Why do MLB pitchers get better or worse? Separating real skill changes
from luck and injury.*

This README covers the data pipeline. See `docs/schema.md` for every table and column.

**Status (2026-11-03):** data v2 is complete and all 11 validation checks pass. Current data:
`gs://mlb-agent-2026/baseball-agent/v2/`. Do not use v1 (it has Baseball Reference for 2026
only).

## Where the data came from and how to load it

### Just use the data (teammates, Cloud Run)

One-time setup: install the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install), then
sign in with the Google account that was given access to the bucket:

```bash
gcloud auth login
```

Download the database (about 164 MB):

```bash
gsutil cp gs://mlb-agent-2026/baseball-agent/v2/baseball.duckdb data/
```

Query it:

```python
import duckdb
con = duckdb.connect("data/baseball.duckdb", read_only=True)
con.sql("SELECT name, season, k_pct, xwoba, woba_minus_xwoba FROM pitcher_season "
        "WHERE season = 2026 AND batters_faced >= 300 ORDER BY xwoba LIMIT 10").show()
```

In the app, call `data_pipeline.db.ensure_db()` at startup. It uses `data/baseball.duckdb` if it
exists, else builds it from `data/processed/*.parquet`, else downloads from
`gs://mlb-agent-2026/baseball-agent/v2/` (env vars `GCS_BUCKET`, `GCS_DATA_VERSION`).

### Access to the GCS bucket

| Who | Role on `mlb-agent-2026` | Can |
|---|---|---|
| Project owner | Owner | everything |
| Teammates | Storage Object Viewer + Storage Object Creator | download; upload new files; cannot overwrite or delete |
| Cloud Run service account | Storage Object Viewer | download at startup |

Storage Object Creator alone cannot download, so teammates need Viewer as well. The owner
grants access in the Cloud Console: *Cloud Storage → Buckets → mlb-agent-2026 → Permissions →
Grant access*.

Data versions are never overwritten: if the data changes, upload it as a new version
(`--version v3`) and set `GCS_DATA_VERSION=v3` on Cloud Run.

The bucket is in **us-east1**. Deploy Cloud Run in `us-east1` too, so the startup download is
fast and free of cross-region transfer charges.

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
uv run python -m data_pipeline.upload_gcs --version v2
```

Raw downloads are cached in `data/raw/` (gitignored) and never re-downloaded; add `--force` to
re-download. Run `fetch_mlb_api` after `fetch_statcast` (it needs the pitcher ids for
`mlb_people`).

### Baseball Reference: saved by hand

Baseball Reference has no API and blocks automated clients, so the 24 league pages were saved
by a person in a normal browser. **All 24 are done** (2015-2025 in `data/raw/bref/manual/`,
2026 in the cache). To add or refresh a season, for the link below either

- save the whole page (Ctrl+S / Cmd+S, "Webpage, HTML only"). Any file name works: the season
  and page are read from the page title, so the browser's default name is fine. Or
- use *Share & Export → Get table as CSV* on the player table and save it as `{season}_{page}.csv`

and put the files in `data/raw/bref/manual/` (on GitHub: open that folder, *Add file → Upload
files*, drag them in, commit). Then run `fetch_bref` (it reads files on disk only;
it never contacts B-Ref unless you pass `--download`).

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
| 2026 | [2026_standard](https://www.baseball-reference.com/leagues/majors/2026-standard-pitching.shtml) | [2026_value](https://www.baseball-reference.com/leagues/majors/2026-value-pitching.shtml) |

A saved HTML page is better than the CSV: it keeps the per-team rows of traded pitchers and the
player id in every row. (Fill in the CSV header names in `CSV_MAP` in `fetch_bref.py` if
B-Ref changes them.)

### Data size

Measured after the full build:

| File | Rows | Size |
|---|---|---|
| `pitches_2024.parquet` | 711,899 | 30.4 MB |
| `pitches_2025.parquet` | 712,528 | 30.8 MB |
| `pitches_2026.parquet` | 716,792 | 27.2 MB |
| other Parquet files | | 3.8 MB |
| `baseball.duckdb` (all tables + summaries) | | 163.6 MB |
| total in `gs://mlb-agent-2026/baseball-agent/v2/` | | 244 MiB |

Cloud Run: the database lives in RAM after download, so 2 GiB memory is enough.

### Validation (v2)

`validate.py`: all 11 checks pass. Spot checks match official totals (Skenes 2024: 170 K;
Skubal 2024: 228 K; Crochet 2025: 255 K), and Statcast strikeouts match B-Ref SO within 2 for
100% of 1,017 pitcher-seasons with 50+ IP (2024-2026).

Baseball Reference rows per season (one row per pitcher; traded pitchers have one `TOT` row):
2015: 735, 2016: 742, 2017: 755, 2018: 799, 2019: 831, 2020: 735, 2021: 909, 2022: 871, 2023: 863, 2024: 855, 2025: 873, 2026: 868.

v1 (2026-11-02) had Baseball Reference for 2026 only; v2 adds 2015-2025. Use v2.

## Layout

```
data_pipeline/   fetch_*.py, build_db.py, validate.py, upload_gcs.py, db.py (ensure_db)
data/raw/        raw downloads + fetch_log.csv (gitignored, except bref/manual/)
data/raw/bref/manual/  Baseball Reference pages saved by hand (2015-2025)
data/processed/  cleaned Parquet (pitches_*.parquet only in GCS: too large for git)
data/baseball.duckdb
docs/schema.md   generated schema for the agent prompts
```
