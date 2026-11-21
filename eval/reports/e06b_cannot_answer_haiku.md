# E6b on Haiku: the `cannot_answer` doc indexed (dev)

Run `20261121T190518Z_e06b_cannot_answer_haiku`: 64 dev questions × 3 repeats on
`claude-haiku-5-5`, effort medium, at commit `806dc1e`. Compared with E6a
(`20261121T185323Z_e06a_refusal_rules_haiku`).

**The one change:** `kb_sources` adds `cannot_answer`, so `search_knowledge` can return the
four sections of `knowledge/docs/cannot_answer.md`. Prompt (`system_v3.md`, same rendered hash),
tool, k and retrieval are as in E6a.

## Headline (dev)

| | E2b | E6a | **E6b** |
|---|---|---|---|
| **Dev pass rate** | 99.5% (98.4–100) | 99.0% (98.4–100) | **99.5%** (98.4–100) |
| Passing runs per repeat | 64, 64, 63 | 63, 64, 63 | 63, 64, 64 |
| Failing runs | q072 r3 | q019 r1, r3 | q019 r1 |
| Tokens per run | 14,431 | 15,523 | 15,066 |
| Cost per run | $0.0010 | $0.0012 | $0.0011 |
| Total cost | $0.20 | $0.22 | $0.22 (estimate $0.25) |
| Latency | 5.3 s | 6.2 s | 6.1 s |

- **No regression beyond repeat variance.** E6b's best repeat (100%) is above E6a's worst
  (98.4%). No question passes fewer runs than in E6a.
- **No gain beyond repeat variance either.** +0.5 points: q019 went from 1/3 to 2/3.
- **q072 stays fixed, 3/3.** Every run opens with "The data doesn't cover today (2026-11-20)".

## Retrieval

| | E6a | E6b |
|---|---|---|
| In-run recall, the 58 questions both configs can score | 90.4% | 90.7% |
| In-run recall, unanswerable (newly in scope) | — | 50.0% |
| In-run recall, all questions | 90.4% | 86.8% |
| Searches per run | 1.28 | 1.21 |

- **The offline crowding didn't happen in the runs.** Offline (question text as the query),
  adding the doc dropped ranking recall from 100% to 70% and trap recall from 86.1% to 76.4%.
  In the agent's own searches, ranking goes from 93.3% to 100% and trap from 80.6% to 77.8%,
  and on the 58 shared questions recall is unchanged. The headline drop to 86.8% comes only from
  the unanswerable questions now counting.
- **Where the doc shows up:** a `cannot_answer` chunk is in the results of 44 of 192 runs, 15 of
  18 unanswerable runs among them. "Only partly answerable" appears 36 times, "Not in the data at
  all" 25, "How to answer" 7 and the intro 4. As offline, "How to answer" is rarely retrieved;
  its heading has no topic words.
- **Unanswerable runs got cheaper:** 16,492 tokens per run against 19,804 in E6a (−17%). With
  the doc in hand the agent explores less before refusing. All 18 still pass, and they name the
  missing data ("The export has no author or account names for the videos you viewed").

## Sub-scores and efficiency (dev, mean per run)

| | E6a | E6b |
|---|---|---|
| Caveat (traps / estimate questions) | 100% / 94.8% | 100% / 97.9% |
| Strong caveat (traps / estimate questions; reported only) | 88.9% / 77.1% | 83.3% / 78.1% |
| Metric selection (reported only) | 64.3% | 59.6% |
| Tool calls / tool errors | 4.98 / 0.047 | 4.84 / 0.052 |
| Steps | 3.14 | 3.10 |

**Over-refusal check:** unanswerable stays at 100%. 10 answerable runs contain a refusal
phrase (8 in E6a), and none fails. As in E6a, these are estimate caveats ("the export has no
video durations") in answers that lead with the number.

## Failure (label confirmed by the owner, 2026-11-22)

| Q | Rep | Label | What happened |
|---|---|---|---|
| q019 | 1 | `wrong_time_window` | "'Last weekend' = Saturday 2026-10-17 and Sunday 2026-10-18 …, the weekend before the current Monday-Sunday week" (gold 24–25 Oct). The same misreading as E1-haiku r2/r3 and E6a r1/r3. The `resolve_dates` stretch item is aimed at it. |

## Implications

- **E6b ends the Haiku ladder at 99.5% on dev**, the same as E2b and Sonnet E1, with the
  refusal failure fixed. Its only remaining failure is q019's "last weekend" reading, which has
  recurred across configs since E1-haiku.
- **Compared with E2b:** the same accuracy at +4% tokens ($0.0011 vs $0.0010 per question).
  It adds the q072 fix, far more strong caveats (estimate questions 33% → 78%), and cheaper
  unanswerable runs. Under the ladder rule (cumulative, a step stays unless it regresses
  beyond variance), **E6b is the best Haiku config** (owner decision 2026-11-22).
- **Final runs on the test split** (32 questions × 3, once each):
  - E6b on Haiku: estimate $0.13 (up to ~$0.18);
  - Sonnet E1: estimate $1.23 (up to ~$1.98).
