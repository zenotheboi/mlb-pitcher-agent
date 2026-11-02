"""Step 7: Validation checks. Prints PASS / FAIL / SKIP for each.

uv run python -m data_pipeline.validate
"""
from __future__ import annotations

import sys

import duckdb

from .common import DB_PATH

SEASONS = [2024, 2025, 2026]
BREF_SEASONS = list(range(2015, 2027))
# check 9: well-known pitchers (MLBAM id, name) -- compare printed values to their Savant pages
SPOT = [(694973, "Paul Skenes"), (669373, "Tarik Skubal"), (676979, "Garrett Crochet")]

results = []


def check(n: int, name: str, ok, detail: str = "") -> None:
    status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    results.append(status)
    print(f"[{status}] {n:>2}. {name}" + (f"  -- {detail}" if detail else ""))


def main() -> None:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    q = lambda sql, *p: con.execute(sql, list(p)).fetchall()  # noqa: E731

    # 1
    counts = dict(q("SELECT season, count(*) FROM pitches GROUP BY 1 ORDER BY 1"))
    ok = all(650_000 <= counts.get(s, 0) <= 800_000 for s in SEASONS)
    check(1, "pitches per season in 650k-800k", ok, str(counts))
    # 2
    dup = q("SELECT count(*) FROM (SELECT 1 FROM pitches GROUP BY game_pk, at_bat_number, "
            "pitch_number HAVING count(*) > 1)")[0][0]
    check(2, "(game_pk, at_bat_number, pitch_number) unique", dup == 0, f"{dup} duplicate keys")
    # 3, 4
    for n, col in [(3, "pitch_type"), (4, "release_speed")]:
        r = q(f"SELECT avg(({col} IS NULL)::INT) FROM pitches")[0][0]
        check(n, f"null rate of {col} < 1%", r < 0.01, f"{r:.3%}")
    # 5
    r = q("""SELECT avg((pl.key_mlbam IS NOT NULL)::INT), count(*) FILTER (WHERE pl.key_mlbam IS NULL)
             FROM (SELECT DISTINCT pitcher FROM pitches) p
             LEFT JOIN players pl ON pl.key_mlbam = p.pitcher""")[0]
    check(5, ">= 99% of pitcher ids match players", r[0] >= 0.99, f"{r[0]:.2%} ({r[1]} unmatched)")
    # 6, 7
    if "bref_pitching_standard" in tables:
        r = q("""SELECT avg((mlbam_id IS NOT NULL)::INT), count(*) FROM bref_pitching_standard
                 WHERE ip_decimal >= 20""")[0]
        check(6, ">= 95% of B-Ref rows with 20+ IP match an MLBAM id", r[0] >= 0.95,
              f"{r[0]:.2%} of {r[1]}")
        have = {s for (s,) in q("SELECT DISTINCT season FROM bref_pitching_standard")}
        missing = sorted(set(BREF_SEASONS) - have)
        check(7, "every season 2015-2026 in bref_pitching_standard", not missing,
              f"missing {missing}" if missing else "")
    else:
        check(6, "B-Ref MLBAM match", None, "bref_pitching_standard not loaded")
        check(7, "B-Ref seasons", None, "bref_pitching_standard not loaded")
    # 8
    il = dict(q("SELECT season, count(*) FROM il_stints GROUP BY 1"))
    neg = q("SELECT count(*) FROM il_stints WHERE days_on_il < 0")[0][0]
    ok = all(counts.get(s) for s in SEASONS) and all(il.get(s) for s in SEASONS) and neg == 0
    check(8, "pitches + il_stints cover 2024-2026, days_on_il >= 0", ok,
          f"il_stints per season {il}, negative={neg}")
    # 9
    print("    9. spot check vs Baseball Savant player pages (compare by hand):")
    for pid, name in SPOT:
        for row in q("SELECT season, pitches, batters_faced, k, round(k_pct, 3), "
                     "round(avg_velo_fastball, 1) FROM pitcher_season WHERE pitcher = ? "
                     "ORDER BY season", pid):
            print(f"       {name:<16} {row[0]}  pitches={row[1]:>5}  BF={row[2]:>4}  K={row[3]:>3}"
                  f"  K%={row[4]}  FBvelo={row[5]}")
    results.append("MANUAL")
    # 10
    if "bref_pitching_standard" in tables:
        r = q("""
            SELECT count(*), avg((abs(ps.k - b.SO) <= 2)::INT)
            FROM bref_pitching_standard b
            JOIN pitcher_season ps ON ps.pitcher = b.mlbam_id AND ps.season = b.season
            WHERE b.ip_decimal >= 50""")[0]
        bad = q("""
            SELECT b.season, b.name, b.SO, ps.k FROM bref_pitching_standard b
            JOIN pitcher_season ps ON ps.pitcher = b.mlbam_id AND ps.season = b.season
            WHERE b.ip_decimal >= 50 AND abs(ps.k - b.SO) > 2 LIMIT 5""")
        check(10, "Statcast K within 2 of B-Ref SO for >= 95% of 50+ IP pitchers",
              r[0] > 0 and r[1] >= 0.95, f"{(r[1] or 0):.2%} of {r[0]}; examples off: {bad}")
    else:
        check(10, "Statcast K vs B-Ref SO", None, "bref_pitching_standard not loaded")

    # extra: sign convention of run value
    r = q("SELECT corr(delta_run_exp, delta_pitcher_run_exp) FROM pitches")[0][0]
    if r is not None:
        check(11, "delta_run_exp is batter-view (corr with delta_pitcher_run_exp ~ -1)",
              r < -0.99, f"corr = {r:.4f}")
    con.close()
    fails = results.count("FAIL")
    print(f"\n{results.count('PASS')} pass, {fails} fail, {results.count('SKIP')} skip")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
