"""The citation check: numbers must come from the rows of the queries a sentence cites."""

from agent.evidence import Evidence, Ledger
from agent.verify import verify


def ledger() -> Ledger:
    led = Ledger()
    for qid, cols, rows in [
        ("Q1", ["season", "k_pct", "xwoba", "avg_velo_fastball", "hr"],
         [[2025, 0.2947, 0.2554, 98.0627, 11], [2026, 0.2809, 0.281, 96.7646, 20]]),
        ("Q2", ["season", "ERA", "FIP"], [[2025, 1.97, 2.36], [2026, 3.82, 3.33]]),
    ]:
        led.items[qid] = Evidence(id=qid, by="test", purpose="t", sql="select 1", columns=cols, rows=rows,
                                  truncated=False, tables=["pitcher_season"], sources=[], elapsed_ms=1)
    return led


def test_exact_percent_and_rounded_values_match():
    r = verify("His K% fell from 29.5% to 28.1% [Q1]. His xwOBA rose to .281 [Q1].", ledger())
    assert r["numbers_checked"] == 3 and r["numbers_matched"] == 3 and r["issues"] == 0


def test_changes_between_cited_values_match():
    # 98.0627 - 96.7646 = 1.3 mph; 20 - 11 = 9 home runs (9 is <= 10, skipped as a count)
    r = verify("His fastball lost 1.3 mph and he allowed 20 home runs [Q1]. ERA rose 1.85 runs [Q2].", ledger())
    assert r["unmatched"] == [] and r["numbers_matched"] == 3


def test_wrong_number_is_flagged():
    r = verify("His xwOBA rose to .291 [Q1].", ledger())
    assert r["issues"] == 1 and r["unmatched"][0]["number"] == ".291"


def test_a_value_rounded_the_wrong_way_is_flagged():
    led = ledger()
    led.items["Q3"] = Evidence(id="Q3", by="test", purpose="t", sql="select 1", columns=["xwoba"], rows=[[0.301482]],
                               truncated=False, tables=[], sources=[], elapsed_ms=1)
    assert verify("His xwOBA was .301 [Q3].", led)["issues"] == 0
    assert verify("His xwOBA was .302 [Q3].", led)["issues"] == 1


def test_a_number_in_the_cited_query_itself_is_supported():
    led = ledger()
    led.items["Q3"] = Evidence(id="Q3", by="test", purpose="League percentiles (min 150 BF)", sql="select 1", columns=["bb_pct"],
                               rows=[[0.061]], truncated=False, tables=[], sources=[], elapsed_ms=1)
    assert verify("Among pitchers with at least 150 batters faced, his rate was 6.1% [Q3].", led)["issues"] == 0
    assert verify("Among pitchers with at least 151 batters faced [Q3].", led)["issues"] == 1


def test_next_questions_arent_checked():
    report = "## Skill vs. luck\n\nHis xwOBA was .281 [Q1].\n\n## Next questions\n\n- Will his .291 hold up?\n"
    r = verify(report, ledger())
    assert r["numbers_checked"] == 1 and r["issues"] == 0


def test_a_section_after_next_questions_is_still_checked():
    r = verify("## Next questions\n\n- Will it last?\n\n## Notes\n\nHe threw 96.8 mph.\n", ledger())
    assert len(r["uncited"]) == 1


def test_precision_is_respected():
    # .284 is not .281: half a unit at 3 decimals is 0.0005
    assert verify("xwOBA of .284 [Q1].", ledger())["issues"] == 1


def test_number_must_be_in_the_cited_query_not_another():
    # 3.82 is in Q2, but the sentence cites Q1
    r = verify("His ERA was 3.82 [Q1].", ledger())
    assert r["unmatched"] and r["unmatched"][0]["cited"] == ["Q1"]


def test_uncited_number_and_unknown_id():
    r = verify("He threw 96.8 mph.\n\nSomething else [Q9].", ledger())
    assert len(r["uncited"]) == 1 and r["unknown_ids"] == ["Q9"]


def test_dates_years_ids_and_il_lengths_are_not_claims():
    r = verify("On 2026-05-22 he left the 60-day IL; in 2026 (id 694973) on May 22 [Q1].", ledger())
    assert r["numbers_checked"] == 0
