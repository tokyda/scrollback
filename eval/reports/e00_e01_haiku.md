# E0-haiku and E1-haiku: the model swap at the base of the ladder

Phase 5 (DESIGN §7.5). E1 left one failing run in 192 on Sonnet dev, so later ladder steps
couldn't show accuracy gains there. The model swap moved from the end of the ladder to its
base: the E0 and E1 configs re-run on `claude-haiku-5-5`, with nothing else changed.

| | E0-haiku | E1-haiku |
|---|---|---|
| Run | `20261120T170650Z_e00_haiku` | `20261120T171107Z_e01_haiku` |
| Config | `eval/experiments/e00_haiku.yaml` | `eval/experiments/e01_haiku.yaml` |
| Change from the Sonnet run | `model: claude-haiku-5-5` | `model: claude-haiku-5-5` |
| Agent | prompt v1, effort medium; describe_table / run_sql / make_chart / final_answer | same, with `describe_include_descriptions: true` |
| Split | dev, 64 questions × 3 repeats (192 runs) | same |
| Cost | $0.16 (estimate was $0.27) | $0.16 (estimate was $0.27) |

Accuracy is the pass rate per repeat, as mean (min–max) across the 3 repeats. All four runs
below use the same scorer: the one in this commit, after the changes listed under "Scoring
changes in this review" (`eval rescore`, no API calls).

## Headline (dev)

| | Sonnet E0 | E0-haiku | Sonnet E1 | E1-haiku |
|---|---|---|---|---|
| **Dev pass rate** | 91.7% (90.6–92.2) | **90.1%** (89.1–90.6) | 99.5% (98.4–100) | **97.9%** (95.3–100) |
| Failing runs | 16 | 19 | 1 | 4 |
| Session-length cluster (q023, q042, q084, q087) | 0/12 | 2/12 | 11/12 | **12/12** |
| Cost per question run | $0.0098 | $0.0009 | $0.0105 | **$0.0008** |
| Cost for 192 runs | $1.87 | $0.16 | $2.01 | $0.16 |

- **Descriptions help Haiku as much as Sonnet:** +7.8 points (E0-haiku → E1-haiku), the same
  as on Sonnet. E1-haiku's worst repeat (95.3%) is above E0-haiku's best (90.6%).
- **E1-haiku is 1.6 points below Sonnet E1 at about 1/13 of the cost per question.** That
  meets target 2 of the Haiku done-when (within about 2 points of Sonnet E1) already at E1.
- Without descriptions Haiku is 1.6 points below Sonnet.

| Dev category | Sonnet E0 | E0-haiku | Sonnet E1 | E1-haiku |
|---|---|---|---|---|
| session_derived | 62.5 | 62.5 | 95.8 | 100 |
| trap | 83.3 | 72.2 | 100 | 94.4 |
| multi_step | 100 | 80.0 | 100 | 93.3 |
| relative_date | 100 | 100 | 100 | 92.6 |
| metric_definition | 83.3 | 88.9 | 100 | 100 |
| ambiguous | 94.4 | 100 | 100 | 100 |
| the other 5 categories | 100 | 100 | 100 | 100 |

**E0-haiku → E1-haiku.**
- Improved (10 questions):
  - session length: q042 0/3→3/3, q087 0/3→3/3, q023 1/3→3/3, q084 1/3→3/3;
  - caveats and no-data: q071 1/3→3/3, q093 2/3→3/3, q072 1/3→2/3;
  - other: q044 2/3→3/3, q052 1/3→2/3, q053 2/3→3/3.
- Regressed: q019 3/3→1/3.

**Sonnet E1 → E1-haiku.** Regressed: q019 3/3→1/3, q052 3/3→2/3, q072 3/3→2/3. Improved:
q087 2/3→3/3.

## Sub-scores and efficiency (dev, mean per run)

| | Sonnet E0 | E0-haiku | Sonnet E1 | E1-haiku |
|---|---|---|---|---|
| execution | 98.8% | 97.1% | 100% | 98.2% |
| answer | 93.0% | 92.4% | 99.4% | 98.2% |
| metric selection (reported only) | 6.4% | 0.6% | 6.4% | 0.0% |
| caveat (traps / estimate questions) | 83.3% / 61.5% | 83.3% / 65.6% | 100% / 80.2% | 100%\* / 89.6% |
| strong caveat (traps / estimate questions; reported only) | 22.2% / 9.4% | 0% / 0% | 44.4% / 42.7% | 55.6% / 18.8% |
| steps / tool calls | 3.02 / 3.96 | 3.14 / 4.46 | 3.02 / 3.96 | 3.11 / 4.28 |
| tool errors | 0.047 | 0.115 | 0.036 | 0.062 |
| tokens | 8,629 | 10,160 | 9,487 | 10,917 |
| latency | 6.0 s | 5.3 s | 6.3 s | 5.2 s |

\* 94.4% as first reported. After the no-data phrase was widened following E2a (2026-11-21), q072
r3 counts as saying there's no data. It still fails, since it also states "0 minutes".

- Haiku makes slightly more tool calls than Sonnet. It uses about 15% more tokens and makes
  more tool errors, but it is faster.
- Haiku labels more estimates ("estimated", "est.") than Sonnet (89.6% vs 80.2% on estimate
  questions). It explains *why* less often (18.8% vs 42.7%).
- Metric selection stays near zero because `metrics_used` holds column expressions, not
  metric names. It becomes meaningful only once `search_knowledge` returns metric names (E2a).

## Failures (labels confirmed by the owner, 2026-11-21)

**E1-haiku: 4 failing runs.**

| Q | Rep | Label | What happened |
|---|---|---|---|
| q019 | 2, 3 | `wrong_time_window` | Asked on Mon 26 Oct; "last weekend" answered as 17–18 Oct (gold 24–25 Oct) |
| q052 | 3 | `aggregation_error` | Rounded the weekly totals in SQL, then subtracted: 413.8 vs gold 413.858, tolerance 0.05 |
| q072 | 3 | `failed_refusal` | Leads with "0 minutes" for today; unknown isn't zero |

**E0-haiku: 19 failing runs.**

| Label | Runs | Questions |
|---|---|---|
| wrong_metric_definition | 11 | session length led by the first-to-last-view span: q023 ×2, q042 ×3, q084 ×2, q087 ×3; q044 r3 (relabelled from the suggested `answer_misreport`: it leads with the 3.9-min span) |
| aggregation_error | 3 | q052 r1/r3 (the same rounding as E1-haiku r3); q053 r2 (the right "7 days", counted from 31 cited rows, not in SQL) |
| missing_caveat | 3 | q071 r1/r2, q093 r3 |
| failed_refusal | 2 | q072 r2 ("0 minutes"), r3 ("0 views for this date") |

The E0-haiku failures match Sonnet E0's main cluster (span instead of estimated watch time).
E1-haiku's failures fall into three areas: relative dates, a no-data refusal, and rounding
inside multi-step SQL. None is about metric definitions.

## Scoring changes in this review

Each change has tests built from these answer texts (`tests/test_eval_score.py`). All six
saved runs were re-scored.

**Owner-approved fixes (scoring false negatives):**
1. "26 Sept" is a date (E1-haiku q087 r1).
2. "Mon 9 – Sun 15 Nov 2026" is a date range, blanked whole before numbers are read, so the 9
   isn't an ungrounded number (q065: E1-haiku r1, E0-haiku r2).
3. "No watch time is recorded for today" is a no-data statement (E0-haiku q072 r1).
4. "est." counts as "estimated" for caveats (q075: E1-haiku r2, E0-haiku r3).
5. **q060 gains the watch-time reading:** the night share of estimated watch time, for the same
   four windows as the view-share readings. This is a gold change (`check-gold --update`) under
   the ambiguity policy (DESIGN §7.1).

**Found while checking item 5 (accepted by the owner, 2026-11-21):** q060's execution check had
been passing on coincidences.
- An integer cell got ±0.5 rounding slack against a ratio, so a cited 0 (`min(local_hour)`,
  `late_flag`) matched any share below one half.
- The hour 16 or 17 in an hourly table matched as "16%" or "17%" (0.16 vs 0.1645, 0.17 vs
  0.1657, tolerance 0.005).
- This had been the case in every run that cited an hourly table, Sonnet's E0 and E1 included.

Two further changes:
6. **No rounding slack for integer cells against a ratio gold, and row-label columns are never
   rescaled.** A row-label column holds distinct consecutive integers over at least 3 rows, such
   as hour of day, day of week or rank.
7. **Shares of a group count as derived values.** Change 6 alone failed correct answers that
   work out the late-night share of watch time from per-hour or per-group totals. A group share
   is sum(column where flag) / sum(column) on a `GROUP BY is_late_night`-style result, and it
   counts only when the text states it.

**Flips from these changes:**

| Run | Before → after (passing runs) | Flips |
|---|---|---|
| Sonnet E0 `20261119T222732Z` | 269 → 269 of 288 (overall 93.4%, dev 91.7%) | The scorer passes q060 r3 ("about 17% of your viewing … 1,955 of 11,885 views, roughly 18 hours of ~109 hours"), since its cited late_min / total_min gives 16.6%. **Owner override, 2026-11-21: stays `answer_misreport`.** The answer computes over views (1,955 / 11,885 = 16.4%), so its match with the watch-time share is a coincidence. Recorded under `overrides` in the label file; `eval rescore` applies it. |
| Sonnet E1 `20261120T050523Z` | 191 → 191 | none (q060 3/3 before and after; it now passes on verified shares rather than the hour coincidence) |
| E-1 `20261119T224207Z` | 155 → 155 | none |
| first E0 run `20261119T212359Z` (superseded) | 217 → 215 of 228 | q060 r1, r3 → fail: their hourly tables have 20 of 24 hours, so the stated 17% can't be derived from what they cite. They had passed through the hour coincidence. Labelled `answer_misreport` (owner, 2026-11-21). |
| E0-haiku | 169 → 173 | q072 r1, q065 r2, q075 r3 (fixes 2–4); q060 r1 (watch-time reading) |
| E1-haiku | 183 → 188 | q087 r1, q065 r1, q075 r2 (fixes 1, 2, 4); q060 r2, r3 (watch-time reading) |

The E0-haiku and E1-haiku numbers in this review's first summary (88.0% and 95.3%) were
before these changes.

## Implications

- **Haiku with descriptions is close to Sonnet.** E1-haiku is 97.9% on dev against 99.5%, at
  about 1/13 of the cost per question.
- **Little accuracy headroom is left on Haiku dev:** 4 failing runs in 192.
  - The primary targets are already met at E1-haiku: the session-length cluster passes 12/12,
    and one trap run fails (q072 r3).
  - The remaining failures (q019 relative dates, q052 rounding, q072 "0 minutes") are the kind
    E3 (examples) or E6 (refusal rules) could move. E2's glossary is unlikely to.
- **Done-when part 1 couldn't be met as written** (E1-haiku has a 100% repeat). The owner
  replaced it on 2026-11-21: part 2 is met at E1-haiku, and from E2a on each step is judged on
  no accuracy regression beyond repeat variance, recall@k, tokens and cost per question, and
  which of the 4 remaining failures it fixes (DESIGN §7.5).
- **Expect E2a and E2b to be judged mostly on tokens, steps, recall@k and the strong-caveat
  sub-score**, not on dev accuracy.
