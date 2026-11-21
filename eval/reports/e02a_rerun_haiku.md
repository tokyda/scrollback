# E2a′ on Haiku: E2a re-run on the new retrieval (dev)

Run `20261121T143825Z_e02a_search_haiku`: the same config as E2a
(`eval/experiments/e02a_search_haiku.yaml`), 64 dev questions × 3 repeats on `claude-haiku-5-5`,
at commit `6833f37`. Compared with the first E2a run (`20261121T070131Z_e02a_search_haiku`).

**The one change is retrieval**, in two commits measured together:
- `4af80ab`: a glossary section's keywords are its heading only.
- `6833f37`: pure numbers aren't keyword tokens.

Prompt, tool, sources and k are unchanged, so the rendered prompt hash is unchanged too. This
re-run is the baseline for E2b, so that E2b changes one thing (owner decision 2026-11-21).

## Offline retrieval (dev, recall@6, E2a's sources)

| | E2a retrieval | `4af80ab` | `4af80ab` + `6833f37` (E2a′) |
|---|---|---|---|
| **all** | 79.3% (40/58 fully found) | 79.3% (39/58) | **81.6%** (42/58) |
| trap | 63.9% | 63.9% | 86.1% |
| metric_definition, ranking, relative_date, simple_agg, time_window | 100% | 100% | 100% |
| Crowding section in a top 6 where it isn't expected | 29 | 28 | 23 |

- **Changed questions** (E2a′ against E2a's retrieval): q071, q074 and q075 now get
  `watch_time_estimation#caveats-to-state-with-any-watch-time-number`, and none lost anything.
- **What caused the crowding:** "Sessions that cross midnight" ranked first by keyword for 17 dev
  questions, 16 of them because of the year in its worked example's dates (`2026-03-06`).
  - The heading-only change alone did little. The numbers rule is the one that moves recall.
  - The section is still in 23 lists, because it's long and full of "session", "watch" and
    "minutes".

## Agent run

| | E2a | E2a′ |
|---|---|---|
| **Dev pass rate** | 98.4% (96.9–100) | **98.4%** (96.9–100) |
| Passing runs per repeat | 62, 64, 63 | 63, 62, 64 |
| Knowledge recall of the agent's searches | 87.6% | 86.6% |
| Runs that searched / searches per run | 97.9% / 1.13 | 98.4% / 1.17 |
| Caveat on estimate questions / strong caveat on estimate questions | 85.4% / 22.9% | 88.5% / 26.0% |
| Tokens per run | 14,401 | 14,653 |
| Cost per run / total | $0.0011 / $0.21 | $0.0011 / $0.21 |

- **No change in accuracy.** q080 went 2/3 → 3/3 and q052 3/3 → 2/3, both within repeat variance.
- **The agent's own search recall didn't move** (87.6% → 86.6%). The offline gain comes from
  question-text queries. The agent writes short concept queries ("watch time duration today"),
  which the year never affected.

## Failures (proposed labels; owner to confirm)

| Q | Rep | Proposed | What happened |
|---|---|---|---|
| q072 | 1 | `failed_refusal` | Leads with "0 minutes", then says there are no views for today (fix C). |
| q072 | 2 | `failed_refusal` | "Zero. … the estimated watch time is 0 minutes." |
| q052 | 2 | `aggregation_error` | Rounded the weekly totals in SQL, then subtracted: 590.8 − 177.0 = 413.8 vs gold 413.858 (tolerance 0.05). The same failure as E1-haiku q052 r3. |

None of these is about retrieval. The two q072 runs are the "unknown isn't zero" failure that E6
targets.
