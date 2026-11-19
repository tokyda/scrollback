# CLAUDE.md

Scrollback is an LLM data agent over the owner's own TikTok watch history. A TikTok JSON export is
ingested into a DuckDB warehouse (raw → staging → intermediate → marts via dbt), with a metrics
layer, data-quality tests and an eval harness for the agent.

- **Spec (source of truth):** [docs/DESIGN.md](docs/DESIGN.md)
- **Build scope and progress:** [docs/ROADMAP.md](docs/ROADMAP.md)

If you change a design decision, update `docs/DESIGN.md` in the same commit and say why.

## Current status (2026-11-20)

**Phases.** Phases 0-4 are done, except the Phase 1 item "ingest one real export": no real
export yet, so every TikTok-format assumption is listed in DESIGN §4.1. When one arrives, put
it in `data/raw/`, run `scrollback ingest` and update that list. An unknown structure fails
with the keys it found (extend `VARIANTS` in `ingest/detect.py` and add a synthetic variant);
unknown extra fields only warn and land in `extras`.

**Open PR.** Phase 4 is PR #5 (branch `phase-4`), not merged; merge only when the owner says.
PRs #1-#3 (Phases 0-2) and #4 (Phase 3) are merged.

**Headline numbers** (96 questions x 3 repeats, `claude-sonnet-5-5`, effort medium; full write-up
in `eval/reports/e00_baseline.md`):

| Run | Overall | Dev | Test (held out) | Cost |
|---|---|---|---|---|
| E0 baseline (marts) `20261119T222732Z_e00_baseline` | **92.7%** | 90.6% | 96.9% | $2.79 |
| E-1 warehouse ablation (staging only) `20261119T224207Z_em1_warehouse_ablation` | **53.5%** | 51.0% | 58.3% | $2.78 |

Same agent, prompt and model; only the queryable tables differ, so the data modelling is worth
about 39 points.

**Confirmed bad-case clusters** (owner-confirmed labels in `eval/labels/`):
- E0, 19 runs:
  - 15 `wrong_metric_definition`, all one cause: session length measured as first-to-last-view
    span instead of estimated watch time (q023, q042, q062, q084, q087, 3/3 each);
  - 3 `missing_caveat` (q075 x2, q071 x1);
  - 1 `answer_misreport` (q060 r3: 16.45% reported as 17%).
- E-1, 133 runs:
  - 53 `over_refusal` ("no durations, here are counts");
  - 65 `wrong_metric_definition` (its own gap estimates, spans, "binge = 30+ videos", views as
    a stand-in);
  - 12 `timezone_or_day_boundary` (sessions cut at midnight);
  - 2 `failed_refusal` (q072 "0 watch events for today");
  - 1 `answer_misreport` (q001: correct text, nothing cited).
- Insight: the marts encode a human decision to estimate watch time that the agent can't make
  alone. E0's remaining failures are exactly the definitions that are not a column.

**Pending:** 3 runs (E0 q034 r2/r3, E-1 q059 r2) are correct sums over many daily rows that the
tightened fix B rejects, because the answer doesn't itemise each row. Their labels are `null`
with a note. Proposed resolution, awaiting the owner: accept a range sum when the answer states
the total and the date range. If accepted, E0 would be 93.4% and E-1 53.8%.

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
- *Fix C / unknown is not zero:* for `no_data` questions (q072), only amounts stated for that
  day count. "No data for today; yesterday 103 min" passes; "0 minutes" or "0 watch events
  for today" fails. The no-data phrases include "no watch events for" and "data ends on".
- *Fix D / grounded numbers:* an unanswerable answer with numbers must lead with a refusal
  (first sentence, no number in it). Every later number must be grounded: a run_sql cell,
  shown arithmetic, a row count, or a describe_table value (row count, column stats, sample
  cells). A hedged guess ("can't know, but probably 40 min") fails.
- *Text matching:* rounding at the printed precision; a ratio also matches as a percentage;
  seconds, minutes and hours scale only with a unit word in text; dates, years, times and
  "N tables" are never answer numbers.
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

**Phase 5 should start with:**
1. Get the owner's decision on the pending range-sum rule. Re-score both runs, and merge PR #5
   once approved.
2. Remember the headroom is small: about 6 failing dev runs per repeat. Accuracy deltas will be
   small, so compare repeats with `eval report <a> <b>` and also track tokens and steps.
3. Run the cheap config switch first: **E1** (`describe_include_descriptions: true`) on dev only.
   Then build the knowledge indexer and `search_knowledge` for **E2**. E2 targets E0's main
   failure, session length by span; the glossary already defines it (`knowledge/docs/sessions.md`).
4. Do **E6** caveat and refusal rules as a new `system_v2.md`; never edit `system_v1.md`.
5. Before E3, write `knowledge/examples.yml` and its overlap check against `eval/questions.yaml`.
6. Every real-API run needs a printed cost estimate and the owner's OK first. Phase 4 cost
   about $7.70 in total; each 96 x 3 run costs about $2.80.

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
uv run scrollback eval report eval/runs/<run> [eval/runs/<other>]
```

Never run `dbt` directly: `dbt_project.yml` has no var defaults on purpose, and
`scrollback build` is what passes `config.yaml` values as `--vars`. To try other parameters,
edit `config.yaml` (or pass `--config`) and rebuild.

If you change the generator, regenerate both samples (the two `synth` commands above) and commit
them; `tests/test_ingest_e2e.py` and CI fail if they drift.

## Code map (built so far)

- `src/scrollback/config.py`: settings loader.
- `src/scrollback/cli.py`: Typer app; `ingest`, `synth`, `build`, `inspect gaps`, `ask` and `eval`
  (`check-gold`, `run`, `rescore`, `report`) are real; `kb`, `serve`, `alerts`, `digest`, `enrich`
  and `eval import-feedback` are stubs.
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
- `src/scrollback/agent/llm.py`: `LLMClient` protocol, `AnthropicLLMClient`, `ScriptedLLMClient`
  (+ `mock_turn`), `create_llm_client` (tests monkeypatch it).
- `src/scrollback/agent/sql_guard.py`: sqlglot guard with stable rule codes.
- `src/scrollback/agent/tools.py`: describe_table, run_sql, make_chart, final_answer + schemas.
- `src/scrollback/agent/loop.py`: `run_agent()`; prompts in `agent/prompts/system_v{N}.md`.
- `src/scrollback/agent/pricing.py`: dated per-model prices; `tracing.py`: JSONL + `agent_traces`.
- `src/scrollback/evals/`: `questions.py` (schema), `warehouses.py` (per-anchor warehouses),
  `gold.py` (gold + snapshot), `score.py` (deterministic scoring), `labels.py`, `run.py`
  (runner, rescore), `report.py`. Data in `eval/` (questions, experiments, labels, reports).

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
