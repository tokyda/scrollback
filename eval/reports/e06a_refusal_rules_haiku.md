# E6a on Haiku: caveat, refusal and "unknown isn't zero" rules (dev)

Run `20261121T185323Z_e06a_refusal_rules_haiku`: 64 dev questions × 3 repeats on
`claude-haiku-5-5`, effort medium, at commit `fc807a5`. Compared with E2b
(`20261121T144258Z_e02b_lookup_rule_haiku`, rescored 2026-11-22 to 99.5%).

**The one change:** the prompt is `system_v3.md`, which is `system_v2.md` plus three rules,
appended to its Rules list. A test checks that this is the only difference. Rendered prompt
SHA-256: `a8da3c45…73fc4c`.
- If a number is an estimate, say so in the answer and give the reason in a few words.
- If the data can't answer the question, say so in the first sentence and name what's missing.
  Don't guess. You may then give a related number the data does have, labelled as such.
- A day or period with no rows is unknown, not zero, if it falls outside the dates the data
  covers. Check the data's first and last dates; if the period is outside them, say the data
  doesn't cover it instead of reporting 0.

Knowledge sources are E2b's (metrics, docs, tables). The `cannot_answer` doc isn't indexed;
that is E6b (owner decision 2026-11-22, DESIGN §7.5).

## Headline (dev)

| | E2b | **E6a** |
|---|---|---|
| **Dev pass rate** | 99.5% (98.4–100) | **99.0%** (98.4–100) |
| Passing runs per repeat | 64, 64, 63 | 63, 64, 63 |
| Failing runs | 1 (q072 r3) | 2 (q019 r1, r3) |
| Session-length cluster (q023, q042, q084, q087) | 12/12 | 12/12 |
| Tokens per run | 14,431 | 15,523 (+8%) |
| Cost per run | $0.0010 | $0.0012 |
| Total cost | $0.20 | $0.22 (estimate $0.24) |
| Latency | 5.3 s | 6.2 s |

- **No regression beyond repeat variance.** E6a's best repeat (100%) is above E2b's worst
  (98.4%), so it stays in the ladder.
- **No gain beyond repeat variance either.** −0.5 points: one question fixed, one lost.

| Dev category | E2b | E6a |
|---|---|---|
| relative_date | 100 | 92.6 |
| trap | 94.4 | 100 |
| the other 9 categories | 100 | 100 |

## What changed

| Q | E1-haiku | E2a | E2a′ | E2b | E6a |
|---|---|---|---|---|---|
| q072 ("0 minutes" for today, no data) | 2 | 1 | 1 | 2 | **3** |
| q019 ("last weekend") | 1 | 3 | 3 | 3 | **1** |

- **q072 is fixed, 3/3, for the first time on any config.** All three runs lead with "The data
  doesn't cover today (2026-11-20)", say the history ends on 2026-11-19, and give yesterday's
  figure, labelled. That is the "unknown isn't zero" behaviour the rule asks for. It is the one
  failure that has persisted since E1-haiku.
- **q019 r1 and r3 fail** (proposed `wrong_time_window`). With today Monday 26 Oct, both read
  "last weekend" as 17–18 Oct instead of 24–25 Oct ("the Mon–Sun week before today"). The SQL
  and arithmetic are correct for the dates they chose. **This is the same misreading, in nearly
  the same words, as E1-haiku q019 r2/r3**, before search existed. The new rules say nothing
  about dates, and E2a–E2b passing 9/9 then E6a 1/3 is consistent with Haiku being unstable on
  this question. I can't rule out that the longer prompt shifts it. If it recurs in E6b, it's
  worth a closer look.

## Sub-scores and efficiency (dev, mean per run)

| | E2b | E6a |
|---|---|---|
| Caveat (traps / estimate questions) | 100% / 91.7% | 100% / 94.8% |
| **Strong caveat, estimate + reason** (traps / estimate questions; reported only) | 66.7% / 33.3% | **88.9% / 77.1%** |
| Knowledge recall of the agent's searches (reported only) | 91.2% | 90.4% |
| Runs that searched / searches per run | 100% / 1.34 | 100% / 1.28 |
| Metric selection (reported only) | 67.8% | 64.3% |
| Tool calls / tool errors | 4.62 / 0.031 | 4.98 / 0.047 |
| run_sql calls per run | 1.08 | 1.16 |
| Runs that query the first or last date | 45 | 70 |
| Steps | 3.16 | 3.14 |

- **Strong caveats more than double on estimate questions** (33% → 77%). Answers now say why
  the number is an estimate ("the export doesn't record how long each view lasted").
- **More coverage checks.** 70 of 192 runs query the first or last date (45 in E2b), which
  explains the extra run_sql calls, the tokens (+8%) and the latency (+0.9 s).
- **Recall** is unchanged within noise (91.2% → 90.4%). The sources are unchanged, as
  intended.

## Over-refusal check

- **Unanswerable questions stay at 100%** (they were already 100% in E2b).
- **No answerable question was refused.** Outside unanswerable and q072, 8 of 171 answerable
  runs contain a refusal phrase (2 in E2b), and all 8 pass and lead with the number:
  - 7 are estimate caveats such as "the export doesn't record how long each view lasted" (q023,
    q038, q044, q084 ×2, q087);
  - 1 is q074 r3, a partial-coverage trap: "The data doesn't cover all of May 2026. It starts on
    2026-05-24", followed by the partial total. That is the behaviour the trap tests.

## Failures (proposed labels; owner to confirm)

| Q | Rep | Proposed | What happened |
|---|---|---|---|
| q019 | 1 | `wrong_time_window` | "Last weekend = Saturday 2026-10-17 and Sunday 2026-10-18" (gold 24–25 Oct). The SQL is correct for those dates. |
| q019 | 3 | `wrong_time_window` | The same reading, "the Mon–Sun week before today, 2026-10-26". |

## Implications

- **E6a fixes the one persistent failure** (q072) and greatly improves caveat quality, at +8%
  tokens and $0.0012 per question. Accuracy is level with E2b within repeat variance.
- **E6b** (+ the `cannot_answer` doc indexed) is next, reported against E6a. Offline, the doc's
  chunks push others out of the top 6 (ranking recall 100% → 70%, trap 86.1% → 76.4%), so the
  thing to watch is whether in-run recall and q019-type questions hold.
