# mlb-pitcher-agent

Deep Research agent: *Why do MLB pitchers get better or worse? Separating real skill changes
from luck and injury.*

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
