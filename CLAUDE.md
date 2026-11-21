# CLAUDE.md

Scrollback is an LLM data agent over the owner's own TikTok watch history. A TikTok JSON export is
ingested into a DuckDB warehouse (raw → staging → intermediate → marts via dbt), with a metrics
layer, data-quality tests and an eval harness for the agent.

- **Spec (source of truth):** [docs/DESIGN.md](docs/DESIGN.md)
- **Build scope and progress:** [docs/ROADMAP.md](docs/ROADMAP.md)

If you change a design decision, update `docs/DESIGN.md` in the same commit and say why.

## Current status (2026-11-22)

**Phases.** Phases 0-5 are done and merged. The first real export is ingested (branch
`real-data`): DESIGN §4.1 records every format assumption against it (it added variant
`v3_likes_favorites`), UTC is confirmed, and the date spine now covers the watch-history window
only (§4.4).

**Real data (2026-11-22).** `config.yaml` points at `data/warehouse/real.duckdb` (git-ignored)
with parameters tuned on the real export: gap 30 min, **cap 120 s** (calibrated against a week
of phone Screen Time; 180 s overshot), last view 10 s, binge 60 min (DESIGN §4.3).
**Tests and evals don't read these:** `repo_settings()` in `tests/conftest.py` and eval runs
pin timezone and metrics to `eval/questions.yaml` `params` (cap 180 s, last view 20 s), so the
synthetic warehouses and the gold never move when `config.yaml` is tuned. Never commit the
owner's per-day Screen Time figures.

**Next:** Phase 6 (Telegram bot).

On real data the privacy rules below apply in full. Print counts and aggregates only, never
records, and keep `AGENT_RESULT_MODE=aggregate_only`. Tests, evals and CI stay on synthetic
data.

**PRs.** Everything through Phase 5 is merged into `main`: #1 (Phases 0-1), #3 (Phase 2), #4
(Phase 3), #5 (Phase 4) and #6 (Phase 5). #2 was superseded by #3.

**Headline results** (96 questions, 64 dev / 32 held-out test, x 3 repeats, effort medium; the
full table is in `README.md`, the final runs in `eval/reports/final_test.md`):

| Config | Model | Dev | Test (held out) | Cost / question |
|---|---|---|---|---|
| E-1 warehouse ablation (staging only) | Sonnet 5.5 | 51.0% | 59.4% | $0.0097 |
| E0 baseline (marts) | Sonnet 5.5 | 91.7% | 96.9% | $0.0097 |
| **E1** (+ descriptions), Sonnet's final config | Sonnet 5.5 | 99.5% | **99.0%** | $0.0105 |
| **E6b** (E1 + search, lookup rule, refusal rules, cannot_answer doc), best Haiku config | Haiku 5.5 | 99.5% | **100%** | $0.0011 |

- The data modelling is worth about 40 points (E-1 vs E0).
- Descriptions fix the session-length definition error on both models.
- Haiku E6b matches Sonnet E1 at about a ninth of the cost.

**Phase 4 baseline** (`claude-sonnet-5-5`; full write-up in `eval/reports/e00_baseline.md`):

| Run | Overall | Dev | Test (held out) | Cost |
|---|---|---|---|---|
| E0 baseline (marts) `20261119T222732Z_e00_baseline` | **93.4%** | 91.7% | 96.9% | $2.79 |
| E-1 warehouse ablation (staging only) `20261119T224207Z_em1_warehouse_ablation` | **53.8%** | 51.0% | 59.4% | $2.78 |

Same agent, prompt and model; only the queryable tables differ, so the data modelling is worth
about 40 points. (Re-scored on 2026-11-21: the scorer now passes E0 q060 r3 on q060's watch-time
reading, but an owner override keeps it a fail, so the numbers are unchanged.) Every failure in
both runs has an owner-confirmed label; nothing is pending.
The scoring false negatives found in review are listed in the label files.

**Confirmed bad-case clusters** (owner-confirmed labels in `eval/labels/`):
- E0, 19 runs:
  - 15 `wrong_metric_definition`, all one cause: session length measured as first-to-last-view
    span instead of estimated watch time (q023, q042, q062, q084, q087, 3/3 each);
  - 3 `missing_caveat` (q075 x2, q071 x1).
  - 1 `answer_misreport` (q060 r3: 16.4% of views reported as "about 17%"; the scorer passes
    it on the watch-time reading, overridden by the owner as a coincidence).
- E-1, 133 runs:
  - 53 `over_refusal` ("no durations, here are counts");
  - 65 `wrong_metric_definition` (its own gap estimates, spans, "binge = 30+ videos", views as
    a stand-in);
  - 12 `timezone_or_day_boundary` (sessions cut at midnight);
  - 2 `failed_refusal` (q072 "0 watch events for today");
  - 1 `answer_misreport` (q001: correct text, nothing cited).
- Insight: the marts encode a human decision to estimate watch time that the agent can't make
  alone. E0's remaining failures are exactly the definitions that are not a column.

**Scoring rules and decisions from Phase 4** (all in DESIGN §7; code in `src/scrollback/evals/`):
- *Pass rules:*
  - normal questions: execution and answer;
  - `trap`: also needs a caveat;
  - `ambiguous`: execution and answer on the **same** reading, plus non-empty assumptions;
  - `unanswerable`: fix D;
  - `no_data`: fix C.
  - `metric_selection` and caveats on non-trap questions are reported only.
- *Execution* uses only the results `final_answer` cites. `execution_strict` = the gold is a
  cell; `execution` also allows arithmetic the answer shows (fix B, tightened).
- *Fix A:* a `wrong_values` number fails only if the answer **leads with it** (states it before
  the competing gold value); labelled context passes. A number matching both is ignored.
- *Fix B (tightened):* a derived value (range sum; +, -, x, / in a row; difference or ratio in
  a column) counts only if the text states it **and every cell used**. That blocks
  coincidences like E-1 q044 (8.1 - 3.9 = 4.2 vs 4.18).
- *Cross-result rule (2026-11-22):* a one-row table gold also matches when each value is a cell
  of some cited result of the run **and** the text states every value (E2b q087 r1).
- *Range-sum rule:* a total over many dated rows also counts when the answer states the total
  **and a date range** (in text or assumptions). The scorer parses the range, sums only the
  cited rows inside it, and the sum must match both the stated total and the gold. This
  resolved E0 q034 r2/r3 and E-1 q059 r2 (confirmed scoring false negatives).
- *Fix C / unknown is not zero:* for `no_data` questions (q072), only amounts stated for that
  day count. "No data for today; yesterday 103 min" passes; "0 minutes" or "0 watch events
  for today" fails. The no-data phrases include "no watch events for", "no TikTok views are
  recorded" and "data ends on".
- *Fix D / grounded numbers:* an unanswerable answer with numbers must lead with a refusal
  (first sentence, no number in it). Every later number must be grounded: a run_sql cell,
  shown arithmetic, a row count, or a describe_table value (row count, column stats, sample
  cells). A hedged guess ("can't know, but probably 40 min") fails.
- *Text matching:* rounding at the printed precision; a ratio also matches as a percentage;
  seconds, minutes and hours scale only with a unit word in text; dates, date ranges ("Mon 9 -
  Sun 15 Nov"), years, times and "N tables" are never answer numbers; "Sept" is a month;
  "est." is an estimate word.
- *Result cells (2026-11-21):* a whole-number cell gets no rounding slack against a ratio; a
  row-label column (distinct consecutive integers, 3+ rows: hour, weekday, rank) is never
  rescaled (hour 17 is not 17%); a share of a group (sum where flag / column total) counts
  when the text states it.
- *Ambiguity policy:* alternatives exist only in `ambiguous`. A question found to have two
  reasonable readings moves there with all readings accepted (q056, q082). Later readings are
  added (q060 10pm-6am).
- *Question set:*
  - every gold must pass the agent's `aggregate_only` guard;
  - unanswerable questions have no proxy in the data;
  - ties at a top-N cutoff need every tied answer as an alternative (none so far);
  - params are pinned in `questions.yaml`;
  - one warehouse per `today` anchor, filtered to dates up to `today`.
- *Reporting:* test accuracy is reported overall only, never per category. The test split is
  only for baselines and final runs.

**Phase 5 (done, merged in #6; ladder rules in DESIGN §7.5):**
1. **Done:** E1 on Sonnet (`describe_include_descriptions: true`), dev 99.5% vs E0 91.7%; one
   failing run (q087 r3). Report: `eval/reports/e01_descriptions.md`. **E1 is Sonnet's final
   config**; re-run Sonnet with a later step only if Haiku results suggest it would help.
2. **Done:** E0-haiku 90.1% and E1-haiku 97.9% (95.3-100) on dev, $0.0008 per question vs
   Sonnet E1's $0.0105. Labels owner-confirmed; report `eval/reports/e00_e01_haiku.md`.
   E1-haiku fails 4 runs: q019 r2/r3 (`wrong_time_window`), q052 r3 (`aggregation_error`),
   q072 r3 (`failed_refusal`). Haiku done-when (replaced 2026-11-21, DESIGN §7.5): within ~2
   points of Sonnet E1 is met; from E2a on, judge each step on no regression beyond repeat
   variance, recall@k, tokens/cost per question, and which of the 4 failures it fixes.
3. **E2a → E6 on Haiku** (owner decision 2026-11-21, although E1-haiku is above ~93%),
   one config change each, cumulative; accuracy first, plus tokens, steps and recall@k. Cost
   estimate and the owner's OK before each run; stop for review after each.
   - **E2a done** (`20261121T070131Z_e02a_search_haiku`, report `eval/reports/e02a_search_haiku.md`):
     dev 98.4% (96.9-100) vs E1-haiku 97.9%, within repeat variance; tokens +32%, $0.0011 per
     question; offline recall@6 79.3%, in-run recall 87.6%. Owner-confirmed failures: q072 r1/r3
     (`failed_refusal`, "0 minutes"), q080 r1 (`wrong_time_window`). q072 r2 was a scoring
     false negative, fixed by widening the no-data phrase ("No TikTok views are recorded").
   - Retrieval (2026-11-21): a glossary section's keywords are its heading only (`4af80ab`):
     recall@6 unchanged at 79.3%. The crowding section ("sessions that cross midnight") is
     lifted mainly by the year in its example dates, so pure numbers are not keyword tokens
     (owner decision): recall@6 81.6%, 3 trap gains, no losses.
   - **E2a′** (E2a re-run on the new retrieval, `20261121T143825Z_e02a_search_haiku`, report
     `eval/reports/e02a_rerun_haiku.md`): 98.4%, same as E2a; offline recall@6 81.6%, in-run
     86.6%. **E2b done** (`20261121T144258Z_e02b_lookup_rule_haiku`, `system_v2.md` =
     v1_search + one rule, report `eval/reports/e02b_lookup_rule_haiku.md`): **99.5%** (98.4-100),
     level with Sonnet E1, $0.0010 per question, in-run recall 91.2%, strong caveats on traps
     44% -> 67%. Only failure: q072 r3 (`failed_refusal`, "0 minutes"). Labels for E2a′ and E2b
     owner-confirmed. Best Haiku config so far: E2b.
   - Scorer (2026-11-22, owner decision, `cfbed84`): a one-row table gold also matches across
     the run's cited results when the text states every gold value (E2b q087 r1, the only
     flip); "no TikTok views for" is a no-data/coverage phrase.
   - **Plan change (2026-11-22, owner):** E3 skipped (no headroom; DESIGN §7.5), E5
     (`query_metric`) moved to the stretch backlog. E6 is split (owner, 2026-11-22), one change
     each. **E6a done** = E2b + `system_v3.md` (v2 + estimate-caveat, refusal-first and
     "unknown isn't zero" rules), same sources (`20261121T185323Z_e06a_refusal_rules_haiku`,
     report `eval/reports/e06a_refusal_rules_haiku.md`): 99.0% (98.4-100) vs E2b 99.5%, within
     variance; q072 fixed 3/3; q019 r1/r3 fail (`wrong_time_window`, 17-18 Oct, as E1-haiku;
     owner-confirmed; a `resolve_dates` tool is in the stretch backlog); strong caveats on
     estimate questions 33% -> 77%; no over-refusal; $0.0012 per question, tokens +8%.
     **E6b done** = E6a + `cannot_answer` in `kb_sources`
     (`20261121T190518Z_e06b_cannot_answer_haiku`, report
     `eval/reports/e06b_cannot_answer_haiku.md`):
     99.5% (98.4-100), no regressions; q072 3/3; fails q019 r1 (label pending); in-run recall on
     shared questions 90.4 -> 90.7% (offline crowding didn't show in runs); $0.0011 per question.
     **E6b is the best Haiku config** (owner, 2026-11-22; q019 r1 confirmed `wrong_time_window`).
   - If a printed estimate exceeds the approved amount, ask before running, even by cents.
   - Glossary edit committed before E2 (`2fa833a`, sessions.md: span vs session length);
     disclose it in the E2a report. `metrics.yml` and the docs predate the eval questions.
   - Owner decisions 2026-11-21: the cell and group-share scorer rules (`94914c9`) accepted;
     E0 q060 r3 overridden to `answer_misreport` (label-file `overrides`, DESIGN §7.3); the
     superseded first E0 run's q060 r1/r3 labelled `answer_misreport`.
   - Knowledge indexer: fastembed, `bge-small-en-v1.5`, in a separate git-ignored DuckDB
     file; `{param}` placeholders are filled at retrieval time from the run's settings.
   - **E2a** = `search_knowledge` only, prompt `system_v1_search.md` (v1 + one tool line, no
     new rule). **E2b** = + the "look up definitions before computing" rule (`system_v2.md`).
     One prompt version = one exact text: `prompt_version` becomes a string, and every trace
     records `system_prompt_sha256` of the rendered prompt.
   - **E3 skipped, E4 cut, E5 stretch** (DESIGN §7.5). Never edit `system_v1.md`. Opus is
     off the ladder.
   - Recall@k needs `expected_knowledge` on each question; show the owner 15 for review first.
4. **Final runs done** (`eval/reports/final_test.md`, at `b257ad8`): test split, once each.
   Haiku E6b `20261121T195214Z` 100% (no failures), $0.0011 per question; Sonnet E1
   `20261121T195447Z` 99.0% (96.9-100), $0.0099 per question, one failure q094 r1
   (`missing_caveat`, owner-confirmed). q062 passes 3/3 on both. **No tuning after test
   results** (owner). README results table written. Merged in #6.
5. The strong-caveat sub-score (estimate + reason) is reported only; it never affects pass.
6. Every real-API run needs a printed cost estimate and the owner's OK first. A Sonnet dev
   run (64 x 3) costs about $2; Haiku is priced at about 1/20 of Sonnet.

## Commands

Everything runs through `uv`.

```bash
uv sync                                   # install deps (incl. dev group)
uv run pytest                             # all tests
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # type check src/ and tests/
uv run scrollback --help                  # CLI

# synthetic data -> raw layer
uv run scrollback synth                   # writes data/sample/user_data_tiktok.json (+ manifest)
uv run scrollback synth --variant v1_activity --out data/sample/user_data_tiktok_v1.json
uv run scrollback ingest data/sample/user_data_tiktok.json [--db path.duckdb] [--no-build]

# warehouse (dbt) and parameter tuning
uv run scrollback build [--db path.duckdb] [-q]   # dbt build with --vars from config.yaml
uv run scrollback inspect gaps [--db path.duckdb] # gap distribution, aggregates only

# agent (needs ANTHROPIC_API_KEY in .env; costs money: ~$0.01-0.02 per question on Sonnet 5.5)
uv run scrollback ask "how many minutes did I watch yesterday" [--show-sql] [--chart] [--today YYYY-MM-DD]

# evals (DESIGN §7): gold check runs in CI; runs cost money and need --yes
uv run scrollback eval check-gold [--update] [--show]
uv run scrollback eval run --config eval/experiments/e00_baseline.yaml [--split dev|test|all] [--repeat 3]   # estimate only
uv run scrollback eval run ... --yes                 # actually runs (E0 96x3 ~ $2.80)
uv run scrollback eval rescore eval/runs/<run>       # re-score saved answers, no API calls
uv run scrollback eval report eval/runs/<run> [eval/runs/<other> --split dev]
uv run scrollback eval retrieval [--k 6] [--split dev] [--experiment <exp.yaml>] [--misses]  # offline recall@k

# knowledge base (Phase 5): index rebuilds itself when knowledge files or the model change
uv run scrollback kb index [--rebuild]
uv run scrollback kb search "screen time" [--k 6] [--text]
```

Never run `dbt` directly: `dbt_project.yml` has no var defaults on purpose, and
`scrollback build` is what passes `config.yaml` values as `--vars`. To try other parameters,
edit `config.yaml` (or pass `--config`) and rebuild.

If you change the generator, regenerate both samples (the two `synth` commands above) and commit
them; `tests/test_ingest_e2e.py` and CI fail if they drift.

## Code map (built so far)

- `src/scrollback/config.py`: settings loader.
- `src/scrollback/cli.py`: Typer app; `ingest`, `synth`, `build`, `inspect gaps`, `ask`, `eval`
  (`check-gold`, `run`, `rescore`, `report`, `retrieval`) and `kb` (`index`, `search`) are real;
  `serve`, `alerts`, `digest`, `enrich` and `eval import-feedback` are stubs.
- `src/scrollback/ingest/detect.py`: declarative format variants; `UnknownExportFormatError`.
- `src/scrollback/ingest/parse.py`: pydantic validation, video ID extraction, `record_hash`.
- `src/scrollback/ingest/load.py`: reads json/zip, idempotent load, `ingest_log`.
- `src/scrollback/warehouse/db.py`: DuckDB connection and raw-layer DDL.
- `src/scrollback/synth/generator.py`: deterministic synthetic export + manifest.
- `src/scrollback/warehouse/dbt_runner.py`: config.yaml → dbt `--vars`; runs dbt in a subprocess.
- `src/scrollback/warehouse/inspect.py`: gap distribution for `inspect gaps`.
- `src/scrollback/semantic/metrics.py`: `knowledge/metrics.yml` schema, loader, `{param}` render.
- `dbt/`: staging (`stg_*`), intermediate (`int_view_gaps`, `int_sessions`), marts (`fct_*`,
  `dim_date`, `agg_daily`), generic test `accepted_range`, singular tests in `dbt/tests/`.
- `knowledge/metrics.yml`: metric definitions; `knowledge/docs/`: glossary docs.
- `src/scrollback/kb/`: `chunks.py` (metric, glossary-section, table and example chunks;
  sources per `agent.kb_sources`), `index.py` (fastembed bge-small in `data/kb/knowledge.duckdb`,
  hash-checked; `hashing` embedder for tests), `retrieve.py` (cosine + BM25, RRF; `{param}`
  filled at retrieval).
- `src/scrollback/agent/llm.py`: `LLMClient` protocol, `AnthropicLLMClient`, `ScriptedLLMClient`
  (+ `mock_turn`), `create_llm_client` (tests monkeypatch it).
- `src/scrollback/agent/sql_guard.py`: sqlglot guard with stable rule codes.
- `src/scrollback/agent/tools.py`: describe_table, run_sql, make_chart, final_answer, and
  search_knowledge when `agent.search_knowledge` is on.
- `src/scrollback/agent/loop.py`: `run_agent()`; prompts in `agent/prompts/system_v{version}.md`
  (`prompt_version` is text: "1", "1_search"); a prompt/tool mismatch fails to load; traces
  record `system_prompt_sha256`.
- `src/scrollback/agent/pricing.py`: dated per-model prices; `tracing.py`: JSONL + `agent_traces`.
- `src/scrollback/evals/`: `questions.py` (schema), `warehouses.py` (per-anchor warehouses),
  `gold.py` (gold + snapshot), `score.py` (deterministic scoring), `labels.py`, `run.py`
  (runner, rescore; per-run `kb_recall`), `report.py`, `retrieval.py` (offline recall@k over
  `expected_knowledge`). Data in `eval/` (questions, experiments, labels, reports).

## Eval conventions

- `eval/questions.yaml` pins its own params; gold changes must be deliberate
  (`check-gold --update` + review the snapshot diff). Write questions and gold SQL from the
  definitions, never by running the agent on them first.
- The test split is held out: run it only for baselines and final results, never to iterate.
- Every real-API eval run prints a cost estimate first and needs the owner's OK (`--yes`).
- Scoring changes need tests with real answer texts, then `eval rescore` on existing runs;
  report any runs that flip. Review failures by hand: separate genuine agent failures from
  suspected scoring false negatives.

## Warehouse and metrics conventions

- Watch time follows DESIGN §4.3 exactly. `tests/test_watch_time_fixture.py` is the
  hand-computed reference; if a model change breaks it, the model is wrong unless DESIGN changes.
- Marts are the only layer the agent may query (`agent.tables: marts`); the only exception is
  the E-1 ablation experiment (`agent.tables: staging`). New columns need a `_marts.yml`
  description.
- A metric's SQL must match what the marts compute: add the metric to `knowledge/metrics.yml`,
  then to the reconciliation in `tests/test_metrics.py` and the hand values in the fixture test.
- Text in `knowledge/` uses `{param}` placeholders for config values, never literal numbers
  (except in clearly labelled worked examples).
- dbt output and `inspect` print names and aggregates only; keep it that way (privacy).
- Tests share one synthetic warehouse built per session (`tests/conftest.py`); use
  `ingest --no-build` in tests that don't need dbt.

## Conventions

- Python 3.11+, src layout (`src/scrollback/`). Follow the repo layout in DESIGN §11.
- Type hints on every function in `src/` (`mypy` runs with `disallow_untyped_defs`). Tests may
  omit return types but are still type-checked.
- `ruff` for lint and format, line length 100. CI fails on any lint, format or type error.
- Tests live in `tests/`, named `test_<module>.py`, with shared fixtures in `tests/conftest.py`
  and static fixture files in `tests/fixtures/`. Write tests with the code, not after.
- Configuration comes from `src/scrollback/config.py` (`load_config()`): defaults <
  `config.yaml` < `.env` < process environment. Don't read env vars elsewhere.
- CLI stubs for unbuilt commands exit with code 2 and name the phase (or "stretch").
- Add dependencies only in the phase that needs them (`uv add <pkg>`, `uv add --dev <pkg>`).
- Small, logical commits. Commit `uv.lock` with dependency changes.

## Agent conventions

- The E0 baseline is deliberately minimal (DESIGN §6.4, §7.5): no metrics.yml or glossary content
  in `system_v1.md`, no descriptions in `describe_table`, no refusal or caveat rules. Improvements
  go in a new prompt version or a config switch so each one is a measurable experiment; never
  edit `system_v1.md` in place once evals exist.
- Current Claude models reject sampling parameters and forced `tool_choice`; use `effort` and
  `tool_choice: auto`. Append assistant turns unchanged. Refusal fallback is off (owner's choice).
- Every new SQL guard rule needs a stable code and a unit test in `tests/test_sql_guard.py`.
- Loop behaviour is tested with `ScriptedLLMClient`; never call the real API in tests or CI.
- Commits and PRs carry no Claude attribution (no co-author trailer, no "Generated with" line).

## Privacy rules (DESIGN §4.6) — non-negotiable

- Real exports go in `data/raw/` only. `data/raw/`, `data/warehouse/*.duckdb`, `.env`, `traces/`
  and `eval/runs/` are git-ignored. Only synthetic data (`data/sample/`) is committed.
- Never print, log or put in test output any real personal record. Errors about export shape
  name **keys only, never values**. CLI output reports counts, not records.
- `source_file` stores the file's basename, not its full path.
- Query results are sent to the LLM provider. `AGENT_RESULT_MODE=aggregate_only` (the default)
  makes the SQL guard reject row-level results and `describe_table` hide `fct_*` rows; keep it on
  for real data. `video_id` and search terms may appear in results (owner's decision).
- Never log or print the API key. It is read once from settings into the SDK client; traces hold
  a non-secret config snapshot only. `traces/` and `charts/` are git-ignored.
- The Telegram bot only answers chat IDs in `TELEGRAM_ALLOWED_CHAT_IDS`.
- Tests, evals and CI run against synthetic data only.
