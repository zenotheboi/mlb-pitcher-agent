# Baseline

Run 2026-11-03 on commit `8d5010a`: `gemini-3.5-flash` for the Clarifier, Lead and Writer, and `gemini-3.5-flash-lite` for the Researchers. Questions W1-W5 in `questions.json`, one try each. Full records are in `logs/runs/` on the machine that ran them, which git doesn't track.

Compare with `uv run python -m evals.run score why`, and add `--commit <id>` to pick a version.

| ID | Verdict | Confidence | Scorer | Facts | Failed queries | Citations | Time | Input tokens | Output tokens |
|---|---|---|---|---|---|---|---|---|---|
| W1 | Real skill gain | High | Pass | 7/8 | 1 of 23 | 63/63 | 71 s | 169,348 | 16,205 |
| W2 | Real skill decline | High | Pass | 6/8 | 1 of 21 | 59/59 | 81 s | 233,305 | 16,102 |
| W3 | Mostly luck | High | Pass | 3/6 | 5 of 31 | 48/48 | 65 s | 368,584 | 15,994 |
| W4 | Mixed: skill + luck | High | Pass | 5/8 | 1 of 23 | 55/55 | 73 s | 309,457 | 15,618 |
| W5 | Mixed: skill + luck | High | **Fail** (confidence) | 2/6 | 4 of 19 | 51/51 | 81 s | 264,186 | 13,255 |
| Total | | | 4/5 | 23/36 | 12 of 117 | 276/276 | | 1,344,880 | 77,174 |

## Problems found by reading the reports

The citation check passed all 276 numbers, and it can't see any of these.

| Report | Problem | Detail |
|---|---|---|
| W3 | Wrong numbers from a bad query | Hard-hit 58.1% to 42.8% and exit velocity 83.4 to 81.4 mph. The database gives 48.4% to 36.5% and 88.9 to 87.7 mph |
| W2 | "Barrel rate" isn't barrels | 30.5% to 32.8%, from a home-made rule (xwOBA of .500 or more). A standard-style rule gives about 10% to 14% |
| W4 | "Barrel rate" isn't barrels | 23.1% to 20.3%, from a home-made rule. A standard-style rule gives about 11% to 9% |
| W5 | Unsupported claim | Says surgery was in March 2026. That's only the IL placement date, and he began a rehab assignment on Mar 28 |
| W5 | A Researcher was lost | Researcher 3 called a tool that doesn't exist (`pitch_overview`) and failed in 1.6 s, so the report has no season K%, xwOBA, ERA or FIP |
| W2 | Meaningless sentences | "From 6 feet to 6 feet" and "from 5 feet to 5 feet", from the Writer's rule to round distances to whole feet |
| W1, W3, W4 | Off by one digit | .302 for .301, 24.4% for 24.3%, 18.2% for 18.1%. Query values are rounded to 4 decimals, then rounded again |
| W1 | Banned word | "elite" |
| W2 | Fake citation | `[Gap]` |
| W5 | Impossible follow-ups | "Next questions" ask about 2027, which isn't in the data |

- Confidence was High on all five, so the label currently tells a reader nothing.
- W1-W4 got the verdict right. W5's verdict label doesn't fit a "full strength" question.
- The barrel figures above use an approximate rule, because the database has no barrel column.

## Queries

All 12 failures were in `run_sql`: 12 of 72 attempts (17%). The prebuilt tools had none in 45.

- 6 of the 12 were the same three column mistakes: `game_month` on the wrong table (2), `WAR` on the standard table (2), and `IP` instead of `ip_decimal` (2).
- Researchers only, round 1 failed 5 of 75 attempts (7%), and later rounds failed 7 of 32 (22%).
- Later rounds use `run_sql` more (88% of attempts, versus 59%), and it also fails more often there (25%, versus 11%).

# Later runs

Same questions, one try each. Scorer results use the expected answers in `questions.json` at the time, so W5 and the overlap checks changed along the way. Read the reports too: the scorer can't judge meaning.

| Version | Models | Verdicts right | Facts | Failed queries | Avg time | Input tokens | Output tokens |
|---|---|---|---|---|---|---|---|
| Baseline `8d5010a` | 3.5 Flash, Flash-Lite | 4/5 | 23/36 | 12 of 117 | 74 s | 1.34M | 77k |
| Stage 1 `04afa53` | 3.5 Flash, Flash-Lite | 2/5 | 20/36 | 8 of 122 | 93 s | 1.53M | 93k |
| Current `f23c05f` | 3.6 Flash, Flash-Lite for the topic check | 5/5 | 32/36 | 2 of 123 | 126 s | 1.32M | 93k |

W2, W3 and W5 also ran on `e2e7e3f`, and M1 on `7d47f86`, between Stage 1 and the current version.

## Current version: what improved

- Every verdict fits the z scores, and W1 corrects the question's premise.
- Injury overlap was right on all five.
- W3 reports Rogers's real stints (48 and 16 days), so the data fix reached the report.
- W5 no longer dates the surgery to March.
- No Researcher was lost, no call fell back, and only 2 queries failed.

## Current version: what's still wrong

| Report | Problem |
|---|---|
| All five | Confidence is High every time, so the label still tells a reader little. W5 fails on this |
| W1 | A home-made strand rate (83.8% to 75.7%), though the data has no such stat. W4 correctly says it isn't available |
| W1 | HR/FB of 17.6% to 23.6%, where the database gives 19.3% to 25.5% |
| W1 | "96.8% average season fastball velocity", the wrong unit |
| W2 | "This lack of control caused his strikeout rate to drop", a causal claim |
| W4 | "Driven by defensive variance", though the data has no defense measure |
| W1, W4, W5 | 1 to 2 numbers left unmatched after the fix step, which the page flags |
| Most | Over the 600-word limit (up to 785 words) |

## With the Critic

Run 2026-11-04 on `a9bb764`, adding the Critic and review loop to the Gemini 3.6 version.

| Measure | Without Critic | With Critic |
|---|---|---|
| Scorer | 4/5 | 3/5 |
| Facts | 32/36 | 33/36 |
| Failed queries | 2 of 123 | 0 of 115 |
| Average time | 126 s | 217 s |
| Input / output tokens | 1.32M / 93k | 1.44M / 124k |

- W1 said Mixed where Real skill gain was expected, and W5 stayed at High confidence.
- The loop worked: 4 reports were revised, 2 of them rewritten, and W2's confidence was capped at Medium over an unmatched number.
- The Critic reported nothing on all five first drafts, though replaying 3 of those drafts found problems in 2. This wasn't explained: the run didn't save each review.
- W4's Lead hit a 429 on Gemini 3.6, and the run recovered without its gap review.

W2 ran again with each review saved (`review_log` in the run record). The Critic found one real problem, the Writer's edit fixed it, and the re-check passed, in 154 s. So the loop works live, but the Critic is inconsistent: it catches some problems each run, and which ones varies.

Causal wording remains, and some of it is fair: these are "why" questions, and "ERA rose due to BABIP" is closer to arithmetic than to a claim about a cause.
