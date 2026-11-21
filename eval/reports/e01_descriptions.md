# E1: table and column descriptions

Phase 5, first step of the experiment ladder (DESIGN §7.5). E1 is the E0 baseline with one
setting changed: `describe_table` also returns the table and column descriptions from
`dbt/models/marts/_marts.yml`.

| | E0 baseline | E1 descriptions |
|---|---|---|
| Run | `20261119T222732Z_e00_baseline` (dev half) | `20261120T050523Z_e01_descriptions` |
| Config | `eval/experiments/e00_baseline.yaml` | `eval/experiments/e01_descriptions.yaml` |
| Change | — | `describe_include_descriptions: true` |
| Agent | prompt v1; describe_table / run_sql / make_chart / final_answer | same |
| Model | `claude-sonnet-5-5`, effort medium | same |
| Split | dev, 64 questions × 3 repeats (192 runs) | same |
| Descriptions | — | `_marts.yml` as committed in Phase 2 (`acbd8c3`), unchanged |

Accuracy is the pass rate per repeat, as mean (min–max) across the 3 repeats. E1 was run on
dev only. Under the ladder rules the test split runs once, on final configs. Both columns use
the current scorer (`eval rescore`, which flipped no runs).

## Headline

| Slice | E0 | E1 | Δ |
|---|---|---|---|
| **Dev** | **91.7%** (90.6–92.2) | **99.5%** (98.4–100) | **+7.8** |

> **Re-scored 2026-11-21** (`eval/reports/e00_e01_haiku.md`): E0 q060 r3 now passes on q060's new
> watch-time reading, so E0 dev is 92.2% (92.2–92.2) and the gain is +7.3. E1 is unchanged.

E1's worst repeat (98.4%) is above E0's best (92.2%), so the gain is beyond repeat
variance, and E1 stays in the ladder.

| Dev category | E0 | E1 |
|---|---|---|
| metric_definition | 83.3% | 100% |
| session_derived | 62.5% (62.5–62.5) | 95.8% (87.5–100) |
| ambiguous | 94.4% (83.3–100) | 100% |
| trap | 83.3% | 100% |
| the other 7 categories | 100% | 100% |

**Improved (7 questions, 15 runs):** q023 0/3→3/3, q042 0/3→3/3, q084 0/3→3/3,
q087 0/3→2/3 (session length), q071 2/3→3/3, q075 1/3→3/3 (caveats), q060 2/3→3/3.
**Regressed:** none.

## Sub-scores and efficiency (dev, mean per run)

| | E0 | E1 |
|---|---|---|
| execution | 98.8% | 100% |
| answer | 92.4% | 99.4% |
| metric selection (reported only) | 6.4% | 6.4% |
| caveat (traps / estimate questions) | 83.3% / 61.5% | 100% / 80.2% |
| strong caveat: estimate + reason (traps / estimate questions; reported only) | 22.2% / 9.4% | 44.4% / 42.7% |
| steps / tool calls | 3.02 / 3.96 | 3.02 / 3.96 |
| tool errors | 0.047 | 0.036 |
| tokens | 8,629 | 9,487 (+10%) |
| latency | 6.0 s | 6.3 s |
| cost | $0.0098 per run, $1.87 total | $0.0105 per run, $2.01 total |

The strong-caveat sub-score was added after this run (DESIGN §7.2) and applied to both runs
by re-scoring.

## What changed in the answers

**Session length (E0's main failure cluster).** In E0 the agent found `est_watch_sec` but
led with the wall-clock span between the first and last view. In E1 every session answer
leads with estimated watch time and gives the span second, labelled as such:

> Your average session in October 2026 was about 10.8 minutes of estimated watch time (138
> sessions). The first-to-last-view span averaged 17.8 minutes. That span leaves out the
> final video's watch time. *(q023 r1)*

The relevant descriptions say what each column is: `end_ts_local` is "local start time of the
last view (observed, not an estimated end)", and `est_watch_sec` on `fct_sessions` is the
"sum of est_watch_sec over the session's views". The agent still computes the span in every
session answer. It passes because fix A lets a wrong-definition value appear as labelled
context after the gold value (DESIGN §7.2). The agent's behaviour changed by the order of two
numbers, not by dropping the span.

**Caveats.** The trap fixes (q071, q075) come from the word "estimated" now appearing in the
answer ("About 60 minutes of estimated TikTok watch time…"), which the caveat check accepts.
Strong caveats rose from 9.4% to 42.7% on estimate questions, but most E1 caveats still don't
say *why* the figure is an estimate.

**The agent read descriptions at the same rate.** It made 377 describe_table calls in E1
against 372 in E0, and 71 against 64 on `fct_sessions`. The gain comes from what those calls
now return, not from more calls.

## Remaining failure (owner-confirmed)

| Q | Rep | Label | Answer |
|---|---|---|---|
| q087 | 3 | `wrong_metric_definition` | "Your longest September session … ran about 76 minutes (75.6 min) … only about 21 minutes of estimated watch time. By estimated watch time, the longest was the session on 26 Sep … 34.8 minutes" |

It leads with the span as the answer to "how long was it". It is a genuine failure, not a
scoring false negative (owner, 2026-11-21).

## Checks

- **No leak from the eval set.** `_marts.yml` has a single commit (`acbd8c3`, Phase 2),
  made before the eval questions (`06cac28`) and before the E0 review. No description was
  edited for this experiment.
- **No scoring change during the comparison.** Both runs were re-scored with the same scorer;
  E0's numbers are unchanged from its baseline report.

## Implications

- **Dev is nearly saturated for Sonnet.** One failing run is left in 192, so later ladder
  steps can't show accuracy gains on Sonnet dev. E1 meets two parts of the Phase 5
  done-when on Sonnet: its mean is above E0's best repeat, and the session-length cluster
  passes 11 of 12 runs.
- **The ladder moves to Haiku** (owner decision, 2026-11-21; DESIGN §7.5). E0 and E1 are
  re-run on `claude-haiku-5-5`. If E1-haiku is below about 93% on dev, E2a–E6 run on Haiku.
  E1 is Sonnet's final config, and the test split runs once at the end for both models.
- The span-as-context pattern, and "estimated" counting as a caveat, are worth watching in
  later runs. Both pass under the current rules, and the strong-caveat sub-score tracks the
  second.
