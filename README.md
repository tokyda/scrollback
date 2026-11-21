# Scrollback

An LLM data agent over your own TikTok watch history. Ask a question in plain English and get an
answer back with the SQL it ran, a chart, and the metric definitions it used.

Status: the warehouse, metrics layer, agent, eval harness and RAG experiment ladder are built
(Phases 0–5); the Telegram bot and polish come next. See [docs/DESIGN.md](docs/DESIGN.md) for
the spec and [docs/ROADMAP.md](docs/ROADMAP.md) for progress.

## Results

96 eval questions (64 dev, 32 held-out test), each run 3 times, scored deterministically
(DESIGN §7). Pass rates are the mean over repeats, with the worst and best repeat in brackets.
Every config is one change from the row above it. Dev was used to iterate; test ran only for
the E0 baseline and the two final configs. All data is synthetic.

| Config | Model | Change | Dev pass rate | Test pass rate | Cost / question | Tokens / question | Latency |
|---|---|---|---|---|---|---|---|
| E-1 | Sonnet 5.5 | E0, but only the staging views (no marts) | 51.0% (50.0–51.6) | 59.4% | $0.0097¹ | 7,341¹ | 7.0 s¹ |
| E0 | Sonnet 5.5 | baseline: schema + `run_sql`, minimal prompt | 91.7% (90.6–92.2) | 96.9% | $0.0097¹ | 8,663¹ | 6.0 s¹ |
| **E1** | Sonnet 5.5 | + table and column descriptions | **99.5%** (98.4–100) | **99.0%** (96.9–100) | $0.0105 | 9,487 | 6.3 s |
| E0 | Haiku 5.5 | E0 on Haiku | 90.1% (89.1–90.6) | — | $0.0009 | 10,160 | 5.3 s |
| E1 | Haiku 5.5 | + table and column descriptions | 97.9% (95.3–100) | — | $0.0008 | 10,917 | 5.2 s |
| E2a | Haiku 5.5 | + `search_knowledge` (hybrid retrieval over metrics, glossary, tables) | 98.4% (96.9–100) | — | $0.0011 | 14,401 | 5.1 s |
| E2a′ | Haiku 5.5 | E2a re-run after two retrieval fixes | 98.4% (96.9–100) | — | $0.0011 | 14,653 | 5.3 s |
| E2b | Haiku 5.5 | + "look up a metric's definition before computing it" | 99.5% (98.4–100) | — | $0.0010 | 14,431 | 5.3 s |
| E6a | Haiku 5.5 | + caveat, refusal and "unknown isn't zero" rules | 99.0% (98.4–100) | — | $0.0012 | 15,523 | 6.2 s |
| **E6b** | Haiku 5.5 | + the "what this data can't answer" doc in the index | **99.5%** (98.4–100) | **100%** (100–100) | $0.0011 | 15,066 | 6.1 s |

¹ E-1 and E0 on Sonnet ran dev and test together; their efficiency figures are over all 96
questions. Other rows are from the dev run.

- **The data modelling is worth about 40 points.** E-1 and E0 differ only in which tables the
  agent may query. The marts encode decisions the agent can't make alone, chiefly how to
  estimate watch time from view start times.
- **Descriptions close most of the rest.** E0's failures were nearly all one definition
  (session length as a clock span instead of estimated watch time); describing the columns
  fixed it on both models.
- **Haiku with retrieval and rules matches Sonnet at about a ninth of the cost.** Haiku E6b and
  Sonnet E1 are level on dev and within one run of each other on test. E3 (few-shot examples)
  and E5 (a semantic-layer tool) were skipped: after E2b there were no failures left for them
  to fix. E4 (richer SQL errors) was cut: there were no SQL-error failures.

**What still fails** (owner-confirmed labels, failing runs out of 192 dev or 96 test):

| Config | Dev | Test |
|---|---|---|
| E-1 Sonnet | 94: 45 over-refusal, 40 wrong metric definition, 6 day-boundary, 2 failed refusal, 1 misreport | 39: 25 wrong metric definition, 8 over-refusal, 6 day-boundary |
| E0 Sonnet | 16: 12 wrong metric definition, 3 missing caveat, 1 misreport | 3: wrong metric definition |
| E1 Sonnet | 1: wrong metric definition | 1: missing caveat² |
| E0 Haiku | 19: 11 wrong metric definition, 3 aggregation, 3 missing caveat, 2 failed refusal | — |
| E1 Haiku | 4: 2 wrong time window, 1 aggregation, 1 failed refusal | — |
| E6b Haiku | 1: wrong time window ("last weekend" read as the weekend before) | 0 |

² Proposed label, awaiting owner confirmation.

Reports for every run are in [eval/reports/](eval/reports/); the final test runs are in
[eval/reports/final_test.md](eval/reports/final_test.md).

## Quickstart (synthetic data)

```bash
uv sync
uv run scrollback synth
uv run scrollback ingest data/sample/user_data_tiktok.json
```

Real exports go in `data/raw/`, which is git-ignored. Only synthetic data is committed.
