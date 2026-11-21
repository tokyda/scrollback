# E2b on Haiku: the lookup rule (dev)

Run `20261121T144258Z_e02b_lookup_rule_haiku`: 64 dev questions × 3 repeats on
`claude-haiku-5-5`, effort medium, at commit `6833f37`. Compared with E2a′
(`20261121T143825Z_e02a_search_haiku`), which is the same retrieval without the rule.

**The one change:** the prompt is `system_v2.md`, which is `system_v1_search.md` plus one rule:
"Before computing any metric, look up its definition with search_knowledge and compute it as
defined." A test checks that this is the only difference. Tool, sources, k and retrieval are as
in E2a′.

The step is judged on the per-step criteria (DESIGN §7.5): no regression beyond repeat variance,
recall@k, tokens and cost per question, and which remaining failures it fixes.

## Headline (dev)

| | E1-haiku | E2a′ | **E2b** |
|---|---|---|---|
| **Dev pass rate** | 97.9% (95.3–100) | 98.4% (96.9–100) | **99.0%** (98.4–100) |
| Failing runs | 4 | 3 | 2 |
| Session-length cluster (q023, q042, q084, q087) | 12/12 | 12/12 | 11/12 (see q087 r1 below) |
| Tokens per run | 10,917 | 14,653 | 14,431 |
| Cost per run | $0.0008 | $0.0011 | $0.0010 |
| Total cost | $0.16 | $0.21 | $0.20 (estimate $0.23–0.24) |

- **No regression beyond repeat variance.** E2b's best repeat (100%) is above E2a′'s worst
  (96.9%), so it stays in the ladder.
- **No gain beyond repeat variance either.** +0.5 points, with repeats of 63, 64 and 63 of 64.
  E2b's worst repeat (98.4%) is above E1-haiku's worst (95.3%) and E2a′'s (96.9%).
- **Haiku against Sonnet E1:** E2b is 99.0% vs 99.5%, at $0.0010 per question vs $0.0105.

## Sub-scores and efficiency (dev, mean per run)

| | E2a′ | E2b |
|---|---|---|
| Runs that searched / searches per run | 98.4% / 1.17 | **100%** / 1.34 |
| Knowledge recall of the agent's searches (reported only) | 86.6% | **91.2%** |
| Metric selection (reported only) | 57.3% | 67.8% |
| Caveat (traps / estimate questions) | 100% / 88.5% | 94.4% / 91.7% |
| Strong caveat, estimate + reason (traps / estimate questions; reported only) | 44.4% / 26.0% | **66.7% / 33.3%** |
| Tool calls / tool errors | 4.90 / 0.052 | 4.62 / 0.031 |
| Steps | 3.16 | 3.16 |
| Latency | 5.3 s | 5.3 s |

- **The rule changes how the agent uses search.**
  - It always searches, slightly more often per run, and finds more of the expected chunks.
  - It cites metric names more often.
  - It explains its estimates more often: strong caveats on traps go from 44% to 67%.
- **Fewer tool calls and errors.** Per run, searches go from 1.17 to 1.34, but `describe_table`
  drops from 1.54 to 1.16 and `run_sql` from 1.19 to 1.12, with fewer errors (0.052 → 0.031).
  Tokens and cost go down slightly.
- **Caveat on traps fell from 100% to 94.4%.** That is one run, q072 r3: "The export has no
  TikTok views for today". The phrase list matches "no views for" but not "no TikTok views for".
  The run fails anyway because it states "0 minutes", and the caveat sub-score is reported only.

## Remaining failures

Across the ladder, passing runs out of 3:

| Q | E1-haiku | E2a | E2a′ | E2b |
|---|---|---|---|---|
| q019 ("last weekend") | 1 | 3 | 3 | 3 |
| q052 (rounding before subtracting) | 2 | 3 | 2 | 3 |
| q072 ("0 minutes" for today) | 2 | 1 | 1 | 2 |
| q080 ("since the start of last month") | 3 | 2 | 3 | 3 |
| q087 (longest session) | 3 | 3 | 3 | 2 |

Only q019 has stayed fixed since E2a. The q052, q072 and q080 counts move within repeat
variance. **q072 is the one persistent failure.** It fails at every step, always the same way
("0 minutes" for a day with no data), and E6 is the step aimed at it.

**E2b failures (proposed labels; owner to confirm):**

| Q | Rep | Proposed | What happened |
|---|---|---|---|
| q072 | 3 | `failed_refusal` | "…so the estimated watch time for today is 0 minutes" (fix C), despite saying the export may not include today. |
| q087 | 1 | **suspected scoring false negative** | The answer is right: "started on Saturday 26 September 2026 at 22:13 … about 34.8 minutes (estimated watch time)". The longest-session minutes (34.78) are in cited result r1, and the date, as a timestamp, is in r2. The one-row table check needs both values in one cited result, and every other q087 run happened to put them in one row. |

**Proposed scorer change for q087 r1 (not applied; needs the owner's OK).** A one-row table gold
would also match when each gold value is in its own cell across the *cited* results. The
long-format rule already allows this within one result, so the change extends it across
results. The text check still applies separately. It would come with a test from this answer, a
rescore of all runs and a flip report. If accepted, E2b would be 99.5% (191/192), level with
Sonnet E1.

## Implications

- **E2b is the best Haiku config so far:** 99.0% on dev at $0.0010 per question. It has the
  highest in-run recall and the most strong caveats, at slightly lower cost than E2a.
- **E3 (examples)** is next. q019 and q080 (relative-date windows) now pass, and q052 passes 3/3
  in E2b, so examples have little left to fix on dev. Their measurable effect would mostly be on
  tokens and steps.
- **E6** is still needed for q072.
- The `examples.yml` overlap check (thresholds to confirm) has to be built before E3 can run.
