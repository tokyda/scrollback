# Scrollback — Roadmap

Build scope by phase. Each phase ends with a "Done when" check. See `docs/DESIGN.md` for the specs referenced as §N.

---

## Phase 0: Foundations
- [x] Repo scaffold matching DESIGN §11, using `uv` and `pyproject.toml`
- [x] `config.py`: loads `config.yaml` + `.env` (timezone, paths, metric params, LLM settings)
- [x] `.gitignore` for real data, the warehouse file, `.env` and eval runs; `.env.example`
- [x] ruff, mypy, pytest set up; GitHub Actions CI running lint + tests
- [x] `CLAUDE.md` with project conventions and commands
- [x] Typer CLI skeleton with stubbed subcommands

**Done when:** `uv run scrollback --help` lists every subcommand, and CI is green.

## Phase 1: Synthetic data and ingestion
- [x] Synthetic generator (§4.5): realistic behaviour, injected events, noise, deterministic seed, at least 2 export format variants
- [x] Commit the generated sample to `data/sample/`
- [x] Format variant detection + tolerant parser (§4.1)
- [x] Pydantic validation; rejects go to `raw_rejects`
- [x] Idempotent load via `record_hash`; `ingest_log`
- [x] Unit tests: each variant, malformed records, video ID extraction, re-ingesting the same file adds 0 rows, overlapping exports merge correctly
- [ ] Ingest one real export locally (never committed) and record anything new about the format in DESIGN §4.1 *(blocked: no real export yet; see the assumption list in DESIGN §4.1)*

**Done when:** `scrollback synth && scrollback ingest data/sample/...` loads the expected row counts, and running it twice changes nothing.

## Phase 2: Warehouse and metrics layer
- [x] dbt-duckdb project; staging, intermediate and mart models (§4.4)
- [x] Session and watch-time estimation (§4.3), parameterised by dbt vars
- [x] `dim_date` spine; `agg_daily` with zero-filled days
- [x] dbt tests (§4.4), including the custom tests
- [x] `knowledge/metrics.yml` with the starter metric set (§5)
- [x] Glossary docs: watch-time estimation, timezone and day boundary, sessions, "what this data cannot answer"
- [x] Hand-built fixture test: about 15 views with hand-computed sessions and est_watch_sec
- [x] `scrollback build` runs `dbt build`
- [x] *(added)* `scrollback inspect gaps` for tuning parameters on a real export; `ingest` runs `dbt build`

**Done when:** `dbt build` passes all tests on synthetic data, and the fixture test matches the hand-computed values exactly.

## Phase 3: Agent v0 (CLI)
- [x] `LLMClient` interface + Anthropic implementation; mock client for tests
- [x] SQL guard (§6.3) with unit tests for every rejection rule
- [x] Tools: `describe_table`, `run_sql`, `make_chart`, `final_answer`
- [x] Tool-calling loop with step limit and structured final answer (§6.1, §6.5)
- [x] Versioned system prompt `system_v1.md`; current date and timezone injected
- [x] Tracing to JSONL + `agent_traces` (§6.6)
- [x] `scrollback ask "..." --show-sql --chart`
- [x] Loop tests with scripted mock tool calls (happy path, SQL error, step limit)

**Done when:** `scrollback ask "how many minutes did I watch yesterday"` returns a correct answer with SQL and a chart, and writes a trace.

## Phase 4: Eval harness and baseline
- [x] `eval/questions.yaml`: 60+ questions across all categories (§7.1), with fixed `today` anchors
- [x] Gold-SQL validity check (runs in CI)
- [x] Scorers: execution accuracy, answer accuracy, metric selection, refusal, caveat, efficiency (§7.2)
- [x] Run, repeat and report commands; run artifacts layout (§7.4)
- [x] Auto-suggested bad-case labels from traces + manual label file (§7.3)
- [x] Run **E0 baseline** ×3; label every failure
- [x] *(added)* Round-2 harder questions (96 total), `eval rescore`, and the **E-1 warehouse ablation** (staging only) as a lower bound; report in `eval/reports/e00_baseline.md`

**Done when:** there's a baseline report with per-category accuracy and a labelled bad-case distribution.

## Phase 5: RAG and optimisation ladder
- [ ] Knowledge indexer: chunks `metrics.yml`, docs, `schema.yml` and examples → embeddings in DuckDB
- [x] `search_knowledge` with hybrid retrieval (embeddings + synonym/keyword match): fastembed bge-small + BM25, RRF; `kb index`, `kb search`
- [ ] ~~`knowledge/examples.yml` few-shot pairs + overlap check against the eval set~~ *(E3 skipped, owner decision 2026-11-22: no headroom; DESIGN §7.5)*
- [x] SQL error self-correction (max 2 retries) *(already part of the E0 baseline, DESIGN §6.3)*
- [ ] ~~Semantic compiler `metrics.yml → SQL` + `query_metric` tool~~ *(moved to the stretch backlog, 2026-11-22)*
- [ ] Refusal and caveat rules in the prompt
- [x] **E1** on Sonnet (dev 99.5% vs E0 91.7%; `eval/reports/e01_descriptions.md`); E1 is Sonnet's final config
- [x] **E0-haiku** and **E1-haiku** on dev (model swap moved to the base of the ladder, §7.5): 90.1% and 97.9% (Sonnet E1 99.5%) at $0.0008 vs $0.0105 per question; `eval/reports/e00_e01_haiku.md`
- [x] **E2a** (98.4% dev after the no-data fix; re-run on the new retrieval as E2a′, 98.4%) and **E2b** (99.5% after the cross-result scorer fix; `eval/reports/e02b_lookup_rule_haiku.md`) on Haiku
- [x] **E6a** on Haiku, built on E2b (`system_v3.md` rules only; done: 99.0%, q072 fixed; `eval/reports/e06a_refusal_rules_haiku.md`), then **E6b** (+ the `cannot_answer` doc in the index; done: 99.5%; `eval/reports/e06b_cannot_answer_haiku.md`); split owner decision 2026-11-22, §7.5. E3 skipped and E5 moved to stretch (owner decision 2026-11-22); E4 cut
- [x] Final: best Haiku config (E6b) and Sonnet E1 on the test split: 100% and 99.0% (E0 96.9%); `eval/reports/final_test.md`
- [x] *(added)* Scorer fixes from the Haiku review ("Sept", weekday date ranges, "est.", a no-data phrase, ratio and row-label cells, group shares) and q060's watch-time reading (§7.2)
- [x] *(added)* Strong-caveat sub-score (estimate + reason; reported only, §7.2) and `eval report a b --split`
- [x] Retrieval eval: for each question, is the right metric or doc in the top k (recall@k) *(run on dev 2026-11-21 with E2a's sources: recall@6 79.3%; `eval/reports/e02a_search_haiku.md`)*
- [x] README results table: accuracy, bad-case mix, tokens and latency per experiment

**Done when:** for Haiku, within ~2 points of Sonnet E1 with cost per question side by side (met at E1-haiku), and from E2a on each step shows no dev regression beyond repeat variance, with recall@k, tokens/cost per question and the remaining failures it fixes reported; for Sonnet, the best config's mean dev pass rate is above E0's best dev repeat and its worst dev repeat is at least E0's dev mean; the dev session-length cluster (q023, q042, q084, q087) passes at least 10 of 12 runs; the final test pass rate is at least E0's 96.9%; tokens per question are reported; and every gain is linked to a run report (DESIGN §7.5).

## Phase 6: Telegram bot
- [x] Telegram bot: free-text questions, chart as photo, "Show SQL" button, allowed chat IDs only (`scrollback serve bot`; DESIGN §8)
- [x] Bot commands: `/today`, `/week` (both about the most recent data, owner decision 2026-11-22), `/metrics`, `/feedback`; `/wrapped` moved to stretch
- [x] `/feedback` attaches to the trace; `scrollback eval import-feedback` turns bad feedback into candidate eval questions (git-ignored `eval/candidates/feedback.yaml`)
- [x] `AGENT_RESULT_MODE=aggregate_only` enforced for real data (the bot refuses to start otherwise)
- [x] *(added)* `ask` and the bot share `interactive.experiment` (Haiku E6b by default) on the calibrated real params; evals unaffected (DESIGN §6.7)
- [ ] Final check from the phone with the real bot and API

**Done when:** asking a question from your phone returns an answer and chart, and bad feedback shows up as a candidate eval question.

## Phase 7: Polish and demo
- [ ] README: problem statement (DESIGN §0), architecture diagram, metric definitions (with the watch-time estimation story), eval results table, demo GIF, privacy note, quickstart using synthetic data
- [ ] Rehearse the 3-minute demo script (DESIGN §8) on real data
- [ ] dbt docs site (lineage graph) linked from the README
- [ ] Short write-up: three most instructive bad cases and how each was fixed
- [ ] Clean-clone check: a fresh clone runs end to end on synthetic data with only an API key

**Done when:** a stranger can clone the repo, run the quickstart, and reproduce the headline eval numbers.

---

## Stretch backlog
- FastAPI service: `/ask`, `/metrics`, `/health`, `/charts/{id}.png`, bearer-token auth (DESIGN §8)
- Monitoring: robust z-score anomaly alerts with a "why?" button, data-health checks, scheduled jobs (DESIGN §9); test that injected synthetic events trigger alerts and normal days don't
- Weekly "Wrapped" digest: deterministic numbers + short LLM narrative, plus the bot's `/wrapped` command (DESIGN §8)
- oEmbed enrichment into `dim_video` + creator metrics (DESIGN §10)
- LangGraph port of the agent loop; compare its eval results with the hand-built loop
- Second source adapter (YouTube Takeout or Spotify extended history), sharing the metrics layer where it fits
- Small web UI (chat + dashboard) on the FastAPI backend
- Multi-turn conversation memory with follow-ups ("and the week before?")
- Clarifying questions for ambiguous queries in interactive mode, with their own eval category
- Cost guardrails: per-day token budget, cheap-model routing for simple questions
- `query_metric` semantic-layer tool (`metrics.yml → SQL`), formerly ladder step E5 (DESIGN §7.5)
- Deterministic relative-date resolver tool, e.g. `resolve_dates("last weekend", today)` → start/end dates, so the agent doesn't do calendar arithmetic itself. Motivated by q019: Haiku reads "last weekend" on a Monday as the weekend before last in some runs (E1-haiku r2/r3, E6a r1/r3)
