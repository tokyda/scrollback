# E0 baseline and E-1 warehouse ablation

Phase 4 baseline for the Scrollback agent (DESIGN §7). Both runs use the same eval set,
scorer and model; E-1 differs from E0 in one setting only.

| | E0 baseline | E-1 warehouse ablation |
|---|---|---|
| Run | `20261119T222732Z_e00_baseline` | `20261119T224207Z_em1_warehouse_ablation` |
| Agent | prompt v1, tools describe_table / run_sql / make_chart / final_answer | same |
| Tables the agent may query | **marts** (`fct_*`, `dim_*`, `agg_*`) | **staging views only** (`stg_*`) |
| Model | `claude-sonnet-5-5`, effort medium | same |
| Result mode | `aggregate_only` | same |
| Eval set | v1, 96 questions (64 dev / 32 test), 8 `today` anchors | same |
| Repeats | 3 (288 runs) | 3 (288 runs) |
| Cost | $2.79 | $2.78 |

Accuracy is the pass rate per repeat, shown as mean (min–max) across the 3 repeats.
Per-category accuracy is reported for **dev only**; the held-out **test** split is reported
overall only, because it has too few questions per category to read into.

## Headline

| Slice | E0 (marts) | E-1 (staging only) | Δ |
|---|---|---|---|
| **Overall** | **93.4%** (92.7–93.8) | **53.8%** (53.1–54.2) | **−39.6** |
| Dev | 91.7% (90.6–92.2) | 51.0% (50.0–51.6) | −40.7 |
| Test (held out) | 96.9% (96.9–96.9) | 59.4% (59.4–59.4) | −37.5 |

> **Re-scored 2026-11-21** (`eval/reports/e00_e01_haiku.md`, "Scoring changes in this review"): q060
> gained a watch-time-share reading, and the scorer stopped matching hour labels and
> whole-number cells as shares. E0 q060 r3 then passed on the watch-time reading, but the
> owner overrode it back to `answer_misreport` (an `overrides` entry in the label file): the
> answer computes over views (1,955 / 11,885 = 16.4%), so its match with the watch-time share
> is a coincidence. **E0 therefore stays at 93.4% overall and 91.7% dev**, test 96.9%, with 19
> failing runs. E-1 is unchanged.

**The data modelling alone is worth about 40 points of accuracy.** The agent, prompt and model are identical; only the tables it can see differ.

## Per category (dev)

| Category | E0 | E-1 | Δ |
|---|---|---|---|
| simple_agg | 100.0 | 91.7 | −8.3 |
| time_window | 100.0 | 60.0 | −40.0 |
| relative_date | 100.0 | 33.3 | −66.7 |
| metric_definition | 83.3 | 66.7 | −16.7 |
| comparison | 100.0 | 0.0 | −100.0 |
| ranking | 100.0 | 100.0 | 0.0 |
| session_derived | 62.5 | 20.8 | −41.7 |
| multi_step | 100.0 | 60.0 | −40.0 |
| ambiguous | 94.4 | 50.0 | −44.4 |
| unanswerable | 100.0 | 100.0 | 0.0 |
| trap | 83.3 | 5.6 | −77.8 |

Ranking and unanswerable questions don't depend on the modelling. Everything that involves
watch time, sessions or calendar days does.

## Sub-scores (all runs)

| Score | E0 | E-1 |
|---|---|---|
| execution (gold in a cited result, or shown arithmetic on one) | 99.2% | 49.6% |
| answer (text states the gold, doesn't lead with a wrong value) | 93.8% | 50.8% |
| refusal correct (unanswerable) | 100.0% | 100.0% |
| caveat present (traps) | 88.9% | 88.9% |
| caveat present (all estimate questions) | 66.7% | 72.6% |
| assumptions stated (ambiguous) | 100.0% | 100.0% |
| metric_selection (informational until E2) | 7.4% | 1.6% |

Efficiency (mean per run): E0 3.02 steps, 3.97 tool calls, 8.7k tokens, 6.0 s, $0.0097.
E-1 3.01 steps, 3.22 tool calls, 7.3k tokens, 7.0 s, $0.0097.

## Bad cases (labels confirmed by the owner)

**E0: 19 failed runs**, all with confirmed labels.

| Label | Runs | Questions |
|---|---|---|
| wrong_metric_definition | 15 | q023, q042, q062, q084, q087 (3/3 each) |
| missing_caveat | 3 | q075 (2/3), q071 (1/3) |
| answer_misreport | 1 | q060 r3 (16.45% of views reported as "about 17%"; the scorer passes it on the watch-time reading since 2026-11-21, overridden by the owner as a coincidence) |

Every E0 definition failure has the same cause: **session length measured as wall-clock span
(first to last view) instead of estimated watch time**. Examples: "the longest session ran
about 62 minutes from start to end" against 34.7 min estimated, or "median session 6.6 min"
against 6.4. The agent finds `est_watch_sec`, but without a definition it leads with the span.
This is the main target for E2 (metrics glossary).

**E-1: 133 failed runs**, all with confirmed labels.

| Label | Runs | What happens |
|---|---|---|
| over_refusal | 53 | Opens with "I can't give minutes watched: the data has no duration", then offers video counts |
| wrong_metric_definition | 65 | Improvises: its own gap-based estimate (e.g. 1,206 vs 1,492.7 min for October), session length by span, "binge = 30+ videos", or videos as a stand-in for watch time |
| timezone_or_day_boundary | 12 | Rebuilds sessions within each calendar day, so sessions are cut at midnight (q043, q044, q047, q048) |
| failed_refusal | 2 | q072: "0 watch events for today": unknown is reported as zero |
| answer_misreport | 1 | q001: correct text, no cited result |

## Insight: the marts encode a human decision

The export records when each video started, never how long it was watched. "Watch time" in
Scrollback exists only because of a **human modelling decision** (DESIGN §4.3): estimate
each view from the gap to the next one, cap it at 3 minutes, impute the last view of a
session, and split sessions at 30-minute gaps. The marts carry that decision as data:
`est_watch_sec`, `fct_sessions`, `is_binge`, `is_late_night`, and the zero-filled `agg_daily`.

Without the marts (E-1), the agent faces the raw fact that there is no duration, and it
behaves like a careful analyst who hasn't been given the definition:

- **53 runs decline.** "I can't give you minutes watched; the data has no duration." From
  the agent's point of view this is honest. From the product's point of view it is an
  **over-refusal**, because the question is answerable under the project's definition.
- **65 runs improvise** a definition of their own: other gap thresholds, wall-clock spans,
  views as a stand-in. The numbers are plausible and wrong.
- Neither path recovers the project's answer, because the deciding information (choosing
  to estimate at all, and how) is a decision, not a fact in the data.

The agent can't make that decision alone, so the modelling layer is not plumbing: it is
where the definitions live. That also explains why E0 is already strong (definitions as
columns), and why E0's remaining failures are exactly the definitions that are *not* a
column: session length (span vs estimated watch time) and when to caveat an estimate.

## Implications for Phase 5

- **Dev headroom over E0 is small:** about 5 failing runs per repeat out of 64 dev
  questions, concentrated in session length (4 dev questions, plus q062 in test) and caveats
  (2 questions).
  E2 (glossary) and E6 (caveat/refusal rules) target exactly these. E1, E3 and E4 have
  little left to improve on accuracy; for them, efficiency (tokens, steps) is the more
  informative comparison.
- **E-1 is a lower bound** showing that each definition delivered to the agent (as
  columns, glossary or the `query_metric` tool) is worth measuring. E2 and E5 are the
  "definitions by text" and "definitions by tool" counterparts of what the marts do.

## How these numbers were scored

Scoring is deterministic (DESIGN §7.2). It was revised during review; every change was
approved by the owner and applied to both runs by re-scoring the saved answers (no API calls):

| Fix | Change | Effect |
|---|---|---|
| A | A wrong-definition value fails only if the answer leads with it | "11,521 different videos, from 11,885 total views" passes as context |
| B | Execution also accepts simple arithmetic on cited cells, **only when the answer shows it** (states the result and every cell used) | "Sat 293 + Sun 186 ≈ 479 min" passes; the q044 coincidence (8.1 − 3.9 = 4.2 vs gold 4.18) doesn't |
| B, range sums | A sum over many dated rows also counts when the answer **states the total and its date range**: the scorer parses the range (text or assumptions), sums only the cited rows inside it, and the sum must match both the stated total and the gold | "this week 189 min vs last week 273 min" with "This week = 2026-11-16 to 2026-11-20" passes; the same totals with a range that doesn't produce them fail |
| C | `no_data`: only amounts stated *for that day* count | "no data for today; yesterday 103 min" passes; "0 minutes today" fails |
| D | Unanswerable: after a leading refusal, numbers must be grounded in the run's query results or describe_table output | real context numbers pass; "I can't know exactly, but probably 40 min" fails |

The question set also changed after the first E0 run: q056, q060 and q082 accept more
readings, q062 lists the span-based median as a wrong value, and round 2 added 20 harder
questions (q077–q096). Gold answers are pinned by `eval/gold_snapshot.json`, and CI checks them.

### Scoring false negatives found in review

All suspected scoring false negatives were confirmed by the owner, fixed in the scorer or the
question set, and re-scored. The last three were correct sums over many daily rows that didn't
itemise each day (E0 q034 r2/r3: "this week 189 min vs last week 273 min"; E-1 q059 r2: "461
videos in the last 7 days, 10–16 Nov"). The range-sum rule above resolves them. They are
recorded under `scoring_false_negatives` in the label files.

## Reproduce

```bash
uv run scrollback eval check-gold
uv run scrollback eval run --config eval/experiments/e00_baseline.yaml --split all --repeat 3          # estimate only
uv run scrollback eval run --config eval/experiments/e00_baseline.yaml --split all --repeat 3 --yes    # ~$2.80
uv run scrollback eval run --config eval/experiments/em1_warehouse_ablation.yaml --split all --repeat 3 --yes
uv run scrollback eval rescore eval/runs/<run>        # after scorer or question changes; no API calls
uv run scrollback eval report eval/runs/<run> [eval/runs/<other run>]
```
