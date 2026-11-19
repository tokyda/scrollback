# CLAUDE.md

Scrollback is an LLM data agent over the owner's own TikTok watch history. A TikTok JSON export is
ingested into a DuckDB warehouse (raw → staging → intermediate → marts via dbt), with a metrics
layer, data-quality tests and an eval harness for the agent.

- **Spec (source of truth):** [docs/DESIGN.md](docs/DESIGN.md)
- **Build scope and progress:** [docs/ROADMAP.md](docs/ROADMAP.md)

If you change a design decision, update `docs/DESIGN.md` in the same commit and say why.

## Current state

Phases 0-4 are done except ingesting a real export; see the roadmap checkboxes. No real
export has been ingested yet, so every TikTok-format assumption is listed in DESIGN §4.1
"Format assumptions to verify against a real export". When a real export arrives, put it in
`data/raw/`, run `scrollback ingest`, and update that list. An unknown structure (or a missing
required field) fails with the keys it found; extend `VARIANTS` in `ingest/detect.py` and add a
synthetic variant. Unknown extra fields only warn and land in the `extras` column; map them via
`FIELD_ALIASES` / `FIELDS` if they matter.

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
- `src/scrollback/cli.py`: Typer app; `synth` and `ingest` are real, the rest are stubs.
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
- Marts are the only layer the agent may query. New columns need a `_marts.yml` description.
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
