# CLAUDE.md

Scrollback is an LLM data agent over the owner's own TikTok watch history. A TikTok JSON export is
ingested into a DuckDB warehouse (raw → staging → intermediate → marts via dbt), with a metrics
layer, data-quality tests and an eval harness for the agent.

- **Spec (source of truth):** [docs/DESIGN.md](docs/DESIGN.md)
- **Build scope and progress:** [docs/ROADMAP.md](docs/ROADMAP.md)

If you change a design decision, update `docs/DESIGN.md` in the same commit and say why.

## Current state

Phases 0 and 1 are done except ingesting a real export; see the roadmap checkboxes. No real
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
uv run scrollback ingest data/sample/user_data_tiktok.json [--db path.duckdb]
```

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

## Privacy rules (DESIGN §4.6) — non-negotiable

- Real exports go in `data/raw/` only. `data/raw/`, `data/warehouse/*.duckdb`, `.env`, `traces/`
  and `eval/runs/` are git-ignored. Only synthetic data (`data/sample/`) is committed.
- Never print, log or put in test output any real personal record. Errors about export shape
  name **keys only, never values**. CLI output reports counts, not records.
- `source_file` stores the file's basename, not its full path.
- Query results are sent to the LLM provider. `AGENT_RESULT_MODE=aggregate_only` (the default)
  makes the SQL guard reject non-aggregate queries; keep it on for real data.
- The Telegram bot only answers chat IDs in `TELEGRAM_ALLOWED_CHAT_IDS`.
- Tests, evals and CI run against synthetic data only.
