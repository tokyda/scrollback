# Scrollback

An LLM data agent over your own TikTok watch history. Ask a question in plain English and get an
answer back with the SQL it ran, a chart, and the metric definitions it used.

Status: the warehouse, metrics layer, agent, eval harness, RAG experiment ladder (Phases 0–5)
and the Telegram bot (Phase 6) are built; polish and the demo come next. See [docs/DESIGN.md](docs/DESIGN.md) for
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
| E1 Sonnet | 1: wrong metric definition | 1: missing caveat |
| E0 Haiku | 19: 11 wrong metric definition, 3 aggregation, 3 missing caveat, 2 failed refusal | — |
| E1 Haiku | 4: 2 wrong time window, 1 aggregation, 1 failed refusal | — |
| E6b Haiku | 1: wrong time window ("last weekend" read as the weekend before) | 0 |


Reports for every run are in [eval/reports/](eval/reports/); the final test runs are in
[eval/reports/final_test.md](eval/reports/final_test.md).

**What these numbers measure, and what they don't.** Every eval run uses synthetic data and the
metric parameters pinned in `eval/questions.yaml` (a 180-second cap per view and 20 seconds for
the last view of a session), so the gold answers never move. `scrollback ask` and the Telegram
bot run the same best config (Haiku E6b) on the owner's real export, where the parameters are
calibrated against phone Screen Time (a 120-second cap and 10 seconds for the last view;
DESIGN §4.3). The agent's definitions and the metric docs follow whichever parameters are
configured, but the accuracy above is measured on the synthetic setup only. Accuracy on real
data is not measured by these numbers; bad answers from the bot feed back into the eval set as
candidate questions instead (see below).

## Telegram bot

Ask questions from your phone. The bot answers only the chat IDs in `TELEGRAM_ALLOWED_CHAT_IDS`
and ignores every other chat without replying. It runs the same config as `scrollback ask`
(`interactive.experiment` in `config.yaml`, Haiku E6b by default) and only in `aggregate_only`
mode, so row-level records never reach the model.

1. Create a bot with Telegram's @BotFather and put its token in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Put your chat ID in `.env` as `TELEGRAM_ALLOWED_CHAT_IDS` (comma-separated for several).
3. Run `uv run scrollback serve bot` and leave it running. Each question costs about $0.001 on
   Haiku.

| You send | You get |
|---|---|
| any question | the answer, its assumptions (including the dates used), the metrics used, a **Show SQL** button and 👍 / 👎 buttons (👎 asks for an optional note); a chart as a photo if you ask for a chart, plot, graph or trend |
| `/today` | watch time on the most recent day in your data (an export always ends before today) |
| `/week` | the last 7 days of data, by day, with a chart |
| `/metrics` | every metric's definition |
| `/feedback good\|bad [note]` | rates the answer you reply to, or the last one, with a note in one message |

`uv run scrollback eval import-feedback` turns answers rated bad into candidate eval questions
in `eval/candidates/feedback.yaml`. That file is git-ignored because it holds real questions and
answers. Candidates are copied into `eval/questions.yaml` by hand, with gold SQL written from the
metric definitions on synthetic data.

## Quickstart (synthetic data)

```bash
uv sync
uv run scrollback synth
uv run scrollback ingest data/sample/user_data_tiktok.json
```

Real exports go in `data/raw/`, which is git-ignored. Only synthetic data is committed.
