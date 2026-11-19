# Scrollback — Design

An LLM data agent over your own TikTok watch history. You ask a question in plain English and get an answer back with the SQL it ran, a chart, and the metric definitions it used.

Underneath sits a small but properly built data warehouse: layered models, a metrics layer, data-quality tests and anomaly alerts. An evaluation harness measures every change to the agent.

---

## 0. Problem statement

TikTok tells you nothing honest about your own usage. The data export gives you a raw log of timestamps and video links. It has no watch durations and no definitions, so you can't answer simple questions like "how much did I actually watch last week?"

Scrollback turns that export into a small data warehouse with explicitly defined metrics. Watch time, for example, is estimated from the gaps between views, with the method documented. On top of that sits an LLM agent that answers questions in plain English and shows the SQL, the definitions and the assumptions behind every number.

The hard part isn't the chatbot. It's making the answers **trustworthy**: consistent metric definitions, refusing questions the data can't answer, and an evaluation set that measures accuracy as the agent improves.

**Scope:** a working, explainable demo. Core build is ingestion → warehouse → metrics layer → agent → evals → Telegram bot. Anything marked *(stretch)* below is optional.

---

## 1. Goals and non-goals

### Goals
1. **Reliable answers about personal viewing behaviour.** Answers must be grounded in SQL, and every metric must be explicitly defined.
2. **A real data foundation.** That means raw → staging → intermediate → marts layers, data-quality tests, and incremental ingestion of repeated exports.
3. **A measurable agent.** A versioned eval set, deterministic scoring, bad-case taxonomy, and experiment tracking. Every claim like "X improved accuracy" must be backed by an eval run.
4. **End-to-end delivery.** A CLI and a Telegram bot. The HTTP API, alerts and weekly digest are stretch goals.
5. **Public-safe repo.** Real personal data never leaves the machine. A synthetic data generator makes the repo, tests and evals reproducible for anyone.

### Non-goals
- A multi-user product, auth system or hosted SaaS.
- Training or fine-tuning models.
- Real-time streaming ingestion. Batch imports of data exports are enough.
- A perfect estimate of watch time. The export has no durations (see §4.3), so the estimate is documented and treated as an estimate.

---

## 2. User stories

| # | As the owner, I want to… | Example |
|---|---|---|
| U1 | ask ad-hoc questions about my viewing | "How many minutes did I watch last Sunday?" |
| U2 | compare periods | "Weekdays vs weekends watch time in October?" |
| U3 | understand habits | "What share of my viewing happens after midnight?" |
| U4 | see the reasoning | the answer shows the SQL, the metric definitions used and the assumptions made |
| U5 *(stretch)* | get proactive alerts | "Yesterday's watch time was 3.1× your 28-day median." |
| U6 *(stretch)* | get a weekly recap | the "Scrollback Wrapped" digest every Monday |
| U7 | get a clear "can't answer" when the data doesn't support it | "Which creator do I watch most?" when enrichment is off |

---

## 3. Architecture

```
            ┌──────────────────────────────────────────────────────────┐
 exports ──▶│ Ingestion  (parse → validate → hash → append raw)        │
 (JSON/zip) └───────────────┬──────────────────────────────────────────┘
                            ▼
            ┌──────────────────────────────────────────────────────────┐
            │ DuckDB warehouse  (dbt-duckdb)                           │
            │  raw_* → stg_* → int_* → fct_/dim_/agg_*  + dbt tests    │
            └───────┬───────────────────────┬──────────────────────────┘
                    │                       │
                    ▼                       ▼
     ┌───────────────────────┐   ┌───────────────────────────────────┐
     │ Knowledge base (RAG)  │   │ Monitoring                        │
     │ metrics.yml, schema   │   │ anomaly detection, weekly digest  │
     │ docs, few-shot SQL    │   └──────────────┬────────────────────┘
     │ → embeddings in DuckDB│                  │
     └──────────┬────────────┘                  │
                ▼                               │
     ┌────────────────────────────────────┐     │
     │ Agent (tool-calling loop)          │     │
     │ search_knowledge · describe_table  │     │
     │ query_metric · run_sql · chart     │     │
     └──────────┬─────────────────────────┘     │
                ▼                               ▼
     ┌────────────────────────────────────────────────────────────┐
     │ Interfaces: CLI (typer) · API (FastAPI) · Telegram bot      │
     └────────────────────────────────────────────────────────────┘
                ▲
     ┌──────────┴─────────────┐
     │ Eval harness + traces  │  questions.yaml → runs/ → reports
     └────────────────────────┘
```

### Tech stack

| Concern | Choice | Why |
|---|---|---|
| Language / env | Python 3.11+, `uv` | fast, reproducible |
| Warehouse | DuckDB | embedded, columnar, SQL-first, no server |
| Transformations | dbt-core + dbt-duckdb | layered models, built-in tests, lineage docs |
| SQL validation | sqlglot | parse, enforce read-only, rewrite LIMIT |
| LLM | Anthropic SDK behind a thin `LLMClient` interface, model set from env | swappable provider |
| Embeddings | sentence-transformers (`BAAI/bge-small-en-v1.5`), local | free, offline, private |
| Vector store | DuckDB table + `array_cosine_similarity` | one store, no extra infra |
| Charts | matplotlib → PNG | works in CLI, API and Telegram |
| CLI | typer | |
| API | FastAPI + uvicorn | |
| Bot | python-telegram-bot | personal, mobile, easy |
| Scheduling | APScheduler inside the bot process (or cron) | |
| Tests | pytest, dbt tests | |
| Lint / format | ruff, mypy (light) | |

---

## 4. Data

### 4.1 Source: the TikTok data export
To get it: TikTok → Settings → Account → Download your data → choose **JSON**.

**The export format is not stable.** Key names and nesting have changed between versions, so the parser must handle several shapes and fail loudly on an unknown one. Known variants (confirm them against a real export):

| Dataset | Typical path(s) | Record fields |
|---|---|---|
| Watch history | `Activity › Video Browsing History › VideoList` or `Your Activity › Watch History › VideoList` | `Date`, `Link` |
| Likes | `Activity › Like List › ItemFavoriteList` or `Your Activity › Like List › ItemFavoriteList` | `Date`/`date`, `Link`/`link` |
| Favourites | `… › Favorite Videos › FavoriteVideoList` | `Date`, `Link` |
| Searches | `… › Search History › SearchList` | `Date`, `SearchTerm` |
| Shares | `… › Share History › ShareHistoryList` | `Date`, `Link`, `Method`, `SharedContent` |

- **Date format** is `YYYY-MM-DD HH:MM:SS`, assumed to be UTC. *Validate this:* check a few views against times you remember. The source timezone is a config value (`SOURCE_TZ`), not hard-coded. The display timezone is a separate config value (`LOCAL_TZ`, default `Asia/Singapore`), used for `local_date`, `local_hour` and so on.
- **Video ID** is extracted from the link with the regex `/video/(\d+)`.
- **Coverage:** the export may only hold a limited history window. Repeated exports overlap, so ingestion must be incremental and idempotent.
- **Input:** a JSON export, either a `.json` file or a `.zip` containing exactly one `.json`. The TXT export is out of scope. *(Changed: §3 previously said JSON/TXT.)*

**Variant detection** (`ingest/detect.py`) is declarative: each variant names an activity root key (`Activity` → `v1_activity`, `Your Activity` → `v2_your_activity`) and a `(section, list)` path per dataset. Record keys match known fields case-insensitively, ignoring spaces and underscores. Detection fails loudly, naming the keys it found and never values, when:
- no known root, or more than one, is present at the top level;
- the root has no watch history section;
- a known section is missing its list key, or the list isn't a list of objects;
- no record in a non-empty list has a required field.

**Unknown extra fields are tolerated.** A record key that doesn't map to a known field for its dataset is logged as a warning (key names only, once per section), stored in the record's `extras` JSON column under its original name, and ingestion continues. Extras never affect `record_hash`, so a later export that adds a field doesn't create duplicates. The CLI prints the warning and `ingest_log.dataset_counts` records the unknown key names per dataset, so a new field gets noticed and can be mapped properly. *(Changed: unknown fields used to fail the whole ingest. A real export that adds a harmless field such as a duration shouldn't block loading; the structural checks above still fail loudly.)*

Missing optional sections (likes, favourites, searches, shares) are allowed and recorded as absent. Unrelated sections such as profile or DMs are ignored. A `null` section or list is treated as empty. An individual record that is missing a field is rejected (§4.2), not treated as a shape error.

#### Format assumptions to verify against a real export
None of these have been checked yet; all were built and tested against synthetic data. Update this list when the first real export is ingested.

1. The export is a single JSON object, delivered as `user_data_tiktok.json` or as a zip containing it.
2. The activity root is `Activity` or `Your Activity`, and exactly one of them is present.
3. The watch history path is `Video Browsing History › VideoList` (v1) or `Watch History › VideoList` (v2).
4. Likes, favourites, searches and shares sit at the same section/list names in both variants (`Like List › ItemFavoriteList`, `Favorite Videos › FavoriteVideoList`, `Search History › SearchList`, `Share History › ShareHistoryList`).
5. Record field names are `Date`, `Link`, `SearchTerm`, `Method`, `SharedContent`, with casing that may vary (e.g. `date`/`link` in likes). Any other field is kept in `extras` with a warning. Check the first real ingest for such warnings: a field like a watch duration would change the watch-time method in §4.3.
6. `Date` is always `YYYY-MM-DD HH:MM:SS`, with no timezone suffix, and the time is UTC.
7. Watch, like and favourite links contain `/video/<digits>`, either as `tiktokv.com/share/video/<id>/` or as `tiktok.com/@<user>/video/<id>`.
8. Share records may point at non-video content (a profile, a live stream, a sound), in which case `Link` has no video ID and `SharedContent` says what was shared.
9. An empty history appears as `[]`, `null` or a missing section, and none of these means a broken export.
10. Record order is unknown (newest first is assumed in the synthetic data); the parser doesn't rely on it.
11. The file is UTF-8, possibly with a BOM.
12. The export itself may contain exact duplicate records.
13. How far back the export reaches (the history window) is unknown.
14. A second-resolution timestamp plus video ID uniquely identifies a view; two genuine views of the same video in the same second don't occur.

### 4.2 Ingestion
`scrollback ingest <path-to-export.json>`

1. Detect the format variant, then extract each dataset into a list of normalised dicts.
2. Validate each record with pydantic: date parses, link present, video ID extractable. Rejected records go to `raw_rejects` with a reason (`missing_date`, `invalid_date`, `missing_link`, `no_video_id`, `missing_search_term`). The link and video ID are required for watch, like and favourite records. For shares they're optional (`video_id` is null for non-video shares).
3. Compute `record_hash = sha256(dataset|timestamp|key)`. `timestamp` is the parsed export timestamp reformatted as `YYYY-MM-DD HH:MM:SS` (no timezone conversion, so changing `SOURCE_TZ` never changes a hash). `key` is the video ID for watch/like/favourite, the normalised search term (NFKC, case-folded, whitespace collapsed) for searches, and the video ID, else the link, else `SharedContent` for shares. *(Changed from hashing the raw date and link strings: the same event can appear with different link shapes across variants, e.g. `tiktokv.com/share/video/<id>` vs `tiktok.com/@user/video/<id>`, and must deduplicate.)*
4. Insert into `raw_*` with `ON CONFLICT DO NOTHING` on `record_hash`, so the step is idempotent. Rejects are keyed by `reject_hash = sha256(dataset|reason|canonical JSON payload)` with the same rule, so re-ingesting a file doesn't duplicate its rejects. *(Added: `raw_rejects` previously had no key.)* Steps 4–5 run in one transaction; shape errors (§4.1) are raised before anything is written.
5. Write one row to `ingest_log` **on every run**: file hash, variant, rows per dataset, how many were new vs duplicate vs rejected. "Idempotent" means `raw_*` and `raw_rejects` don't change on a re-run; the log still records the attempt, with `rows_new = 0`. *(Clarified: the roadmap's "running it twice changes nothing" refers to the data tables, not the audit log.)* "Duplicate" counts valid records that weren't inserted, either because they repeat within the file or because they're already loaded.
6. Run `dbt build`. *(Wired in Phase 2. Until then `ingest` prints that the step was skipped.)*

### 4.3 Core metric problem: there is no watch duration
The export records **when a view started**, not how long it lasted, so watch time has to be estimated. The method is versioned in `metrics.yml` and parameterised by dbt vars. The parameter values live in `config.yaml` (`metrics:`) as the single source of truth, and `dbt_runner` passes them to dbt as `--vars`; `dbt_project.yml` only holds fallback defaults. *(Clarified: previously both files looked authoritative.)*

- **Session:** an ordered run of views where the gap between consecutive views is ≤ `session_gap_min` (default **30 min**).
- **Per-view estimated watch seconds:**
  - not the last view in its session: `min(gap_to_next_view_sec, view_cap_sec)`, with `view_cap_sec` default **180**
  - last view in its session: the median of the other estimates in that session, falling back to `default_last_view_sec` (default **20**)
- **Caveats, which the agent must surface when relevant:** pauses within the cap are counted as watching, long videos are undercounted at the cap, and background play is invisible.

This estimate is the project's main data-definition story. It belongs in the README, and the agent cites it whenever "watch time" appears.

### 4.4 Warehouse models

**raw** (loaded as-is plus load metadata)
Every raw `*` table has an `extras JSON` column (NULL unless the record had unknown fields, see §4.1), and ends with the load metadata `source_file VARCHAR` (the file's basename, never a full path), `ingest_id BIGINT` (→ `ingest_log`) and `loaded_at TIMESTAMP` (UTC). `video_id` is derived at ingest because the record hash depends on it; everything else is stored as written.
- `raw_watch(record_hash PK, event_ts_raw VARCHAR, link VARCHAR, video_id VARCHAR, …metadata)`
- `raw_like`, `raw_favorite`: same columns as `raw_watch`
- `raw_search(record_hash PK, event_ts_raw VARCHAR, search_term VARCHAR, …metadata)`: the term as written; normalisation happens in staging
- `raw_share(record_hash PK, event_ts_raw VARCHAR, link VARCHAR, video_id VARCHAR NULL, method VARCHAR, shared_content VARCHAR, …metadata)`
- `raw_rejects(reject_hash PK, dataset, payload JSON, reason, …metadata)`: the payload is the original record with its original keys
- `ingest_log(ingest_id PK, source_file, file_sha256, variant, started_at, finished_at, rows_read, rows_new, rows_duplicate, rows_rejected, rejects_new, dataset_counts JSON)`: `dataset_counts` maps each dataset to `{present, read, valid, rejected, new, duplicate, unknown_fields}`, where `unknown_fields` holds key names only

**staging** (typed, cleaned, deduped; one model per raw table)
- `stg_watch(view_id, video_id, event_ts_utc, event_ts_local, local_date, local_hour, dow)`
- `stg_like(video_id, event_ts_utc, event_ts_local, local_date)`
- `stg_search(search_term, search_term_norm, event_ts_utc, event_ts_local, local_date)`

**intermediate**
- `int_view_gaps`: each view joined to the next view's timestamp, plus `gap_sec`
- `int_sessions`: session assignment via a running sum of "new session" flags

**marts** (the only layer the agent may query)
- `fct_views(view_id, video_id, session_id, event_ts_local, local_date, local_hour, dow, is_weekend, is_late_night, est_watch_sec, is_rewatch)`
- `fct_sessions(session_id, start_ts_local, end_ts_local, local_date, n_views, est_watch_sec, is_binge)`
- `fct_likes(video_id, event_ts_local, local_date, liked_after_watch BOOL)`
- `fct_searches(search_term_norm, event_ts_local, local_date)`
- `dim_date(local_date, dow, dow_name, is_weekend, iso_week, month, year)`: a spine covering the full data range, so zero-activity days exist
- `agg_daily(local_date, views, est_watch_min, sessions, longest_session_min, late_night_views, likes, searches)`: built from `dim_date` with left joins, so missing days are 0, not absent
- `dim_video(video_id, title, author_name, enriched_at)`: optional, filled by enrichment (Phase 8)

**dbt tests**
- `unique` / `not_null` on keys
- `accepted_range`: `est_watch_sec` between 0 and `view_cap_sec`, `local_hour` between 0 and 23
- relationships: `fct_views.session_id → fct_sessions`
- custom: no `local_date` in the future; `agg_daily` row count equals the `dim_date` span; daily views don't exceed a sanity bound (e.g. 3,000)
- freshness: warn if the latest event is more than N days old

### 4.5 Synthetic data generator
`scrollback synth --days 180 --seed 42 --end 2026-11-19 --variant v2_your_activity --out data/sample/user_data_tiktok.json`

- Writes the **same JSON shape** as a real export, one variant per `--variant` flag (`v1_activity` or `v2_your_activity`), so the real parser is exercised. The variants differ in their activity root and watch-history path; v2 also uses lowercase `date`/`link` keys and `tiktok.com/@user/video/<id>` links for likes and favourites. Unrelated decoy sections (profile, DMs, comments) are included so detection must ignore them. *(Added `--variant`.)*
- The window ends on `--end`, which defaults to the **fixed** date `2026-11-19` rather than today, so output never depends on when it runs and matches the eval `today` anchors. *(Added `--end`.)*
- **Deterministic universe:** each behavioural day (05:00 to 04:59 local) uses its own RNG seeded by `(seed, date)`. An export is a time window over that universe, so the same seed and window give byte-identical output, and a shorter or earlier window is an exact subset of a longer one. That models repeated, overlapping real exports.
- Behaviour happens in the synthetic person's timezone (`--local-tz`, default `Asia/Singapore`), and timestamps are written in UTC, as a real export is assumed to be.
- Writes `<name>.manifest.json` next to the export, holding the seed, window, variant, timezones, the injected events in the window and the **expected counts per dataset** (`records`, `valid_unique`, `duplicates`, `rejected`). The counts come from the generator's own events, not from re-parsing, and ingestion tests compare against them.
- Committed samples (seed 42, 180 days): `data/sample/user_data_tiktok.json` (v2) and `data/sample/user_data_tiktok_v1.json` (v1), each with a manifest. A test fails if they drift from the generator.
- Behaviour model:
  - weekday/weekend and hour-of-day intensity curves (evening peak, some late-night use)
  - session count per day ~ Poisson, views per session ~ lognormal, gaps between views ~ lognormal
  - injected events for testing anomaly detection and evals: a binge weekend, a "detox" week with near-zero use, a holiday spike
  - likes at ~3–6% of views, searches with a small vocabulary of terms (some typed with different case or trailing spaces)
  - a small number of favourites and shares, including non-video shares (a creator profile link), so every raw table is exercised *(added)*
  - rewatches (about 3% of views repeat a video seen earlier that day) and a few likes of videos absent from watch history
  - noise: exact duplicate records, a few malformed or empty dates, links with no video ID, a record with no link
  - injected events are fixed calendar dates: `holiday_spike` 2026-08-09, `detox_week` 2026-09-07..13, `binge_weekend` 2026-10-24..25
- Deterministic for a given seed. **Evals and CI run against this data only.**

### 4.6 Privacy
- `data/raw/`, `data/warehouse/*.duckdb` and `.env` are git-ignored. Only `data/sample/` (synthetic) is committed.
- **What the LLM sees:** query results are sent to the LLM provider. Config `AGENT_RESULT_MODE=aggregate_only` makes the SQL guard reject queries without GROUP BY or aggregation (on by default for real data).
- The Telegram bot only answers chat IDs listed in `TELEGRAM_ALLOWED_CHAT_IDS`.

---

## 5. Metrics layer: `knowledge/metrics.yml`

The single source of truth for metric definitions. It's used in three places:
1. RAG documents for the agent
2. the `query_metric` tool, which compiles the definition to SQL
3. the generated docs page

```yaml
metrics:
  - name: est_watch_minutes
    label: Estimated watch time (minutes)
    description: >
      Sum of per-view estimated watch seconds / 60. Estimated from gaps between
      consecutive views because the export has no durations. See "watch time estimation".
    sql: SUM(est_watch_sec) / 60.0
    table: fct_views
    time_column: local_date
    default_grain: day
    caveats: [estimate, pauses_counted_up_to_cap]
    synonyms: [watch time, screen time on tiktok, minutes watched, time spent]

  - name: active_days
    label: Active days
    description: Number of local calendar days with at least one view.
    sql: COUNT(DISTINCT local_date)
    table: fct_views
    time_column: local_date
    synonyms: [days i used tiktok, dau-equivalent]
```

**Starter metric set:**

| Metric | Definition |
|---|---|
| `views` | count of view events |
| `unique_videos` | distinct video_id |
| `rewatch_rate` | views of a video_id seen before ÷ views |
| `est_watch_minutes` | above |
| `active_days` | above |
| `active_days_7d` | active days in a trailing 7-day window (the single-user analogue of DAU/WAU) |
| `sessions` | count of sessions |
| `avg_session_minutes` | mean session est_watch_sec ÷ 60 |
| `binge_sessions` | sessions with est watch ≥ `binge_threshold_min` (default 60) |
| `late_night_share` | views with local_hour in [0, 5) ÷ views |
| `weekend_share` | weekend views ÷ views |
| `likes`, `like_rate` | likes; likes ÷ views in the same period |
| `searches` | count of searches |
| `streak_days` | longest run of consecutive active days |

**Glossary docs** (`knowledge/docs/*.md`) cover concepts that aren't single metrics: the watch time estimation method, the timezone and day-boundary rule, the definition of a session, what the data **cannot** answer (true durations, creators without enrichment, video content, ads), and table and column descriptions (generated from dbt `schema.yml`).

**Few-shot examples** (`knowledge/examples.yml`) are verified question → SQL pairs. They must never overlap with eval questions; a CI check enforces this with normalised-text and embedding similarity.

---

## 6. Agent

### 6.1 Loop
A plain tool-calling loop. No framework, so every step is visible and debuggable.

```
system prompt (role, rules, current date + timezone, table list)
→ LLM ⇄ tools (max N steps, default 8)
→ final_answer(text, sql_used[], metrics_used[], assumptions[], chart_path?)
```

### 6.2 Tools

| Tool | Input | Output | Notes |
|---|---|---|---|
| `search_knowledge` | `query`, `k` | top-k chunks (metrics, docs, examples) | hybrid: embedding similarity + keyword match on synonyms |
| `describe_table` | `table` | columns, types, descriptions, 3 sample rows | marts only |
| `query_metric` | `metric`, `grain`, `start`, `end`, `filters`, `group_by` | rows + compiled SQL | compiles from `metrics.yml`; the preferred path |
| `run_sql` | `sql` | rows (≤ 200), column names, row count, or error | read-only guard below |
| `make_chart` | `rows`, `kind` (line/bar), `x`, `y`, `title` | PNG path | |
| `final_answer` | structured | — | ends the loop |

### 6.3 SQL guard (`run_sql`)
- Parse with sqlglot (DuckDB dialect). Reject anything that isn't a single `SELECT`/`WITH`.
- Table allowlist: `fct_*`, `dim_*`, `agg_*`.
- Force `LIMIT 200` if none is given, plus a statement timeout.
- Read-only DuckDB connection (`read_only=True`).
- Errors are returned to the model as structured text so it can self-correct, with a maximum of 2 retries per query.

### 6.4 System prompt rules (abridged)
- Always resolve metric definitions through `search_knowledge` before computing anything.
- Prefer `query_metric`, and fall back to `run_sql` for anything it can't express.
- Resolve relative dates ("last week") against the supplied `today` and timezone. State the resolved range in `assumptions`. Weeks run Monday to Sunday.
- If a metric is an estimate, say so.
- If the data can't answer the question, say what's missing. Don't guess.
- If the question is ambiguous, pick the most reasonable reading and state it. Don't ask questions back in single-shot mode.

### 6.5 Answer format
```
You watched ~142 min last Sunday (15 Nov), your 3rd-highest day this month.

Metric: est_watch_minutes (estimated from gaps between views; pauses up to 3 min count as watching)
Range: 2026-11-15, Asia/Singapore
SQL: <collapsed>
[chart]
```

### 6.6 Tracing
Every run writes one JSON line to `traces/` and a row to `agent_traces` in DuckDB. Each record holds the question, model, prompt version, every tool call (name, args, duration, error), the final answer, tokens, cost estimate and latency. Traces are the raw material for bad-case analysis.

### 6.7 Config and versioning
- The prompt lives in `src/scrollback/agent/prompts/system_v{N}.md`, and the version is recorded in traces and eval runs.
- Model, temperature (default 0), max steps, retrieval k and tool on/off flags all come from `config.yaml` and env, so experiments are config diffs, not code diffs.

---

## 7. Evaluation

### 7.1 Eval set: `eval/questions.yaml`
```yaml
- id: q017
  question: "What share of my views happened after midnight in October 2026?"
  category: metric_definition
  difficulty: medium
  today: "2026-11-19"          # fixed anchor for relative dates
  gold_sql: |
    SELECT AVG(CASE WHEN local_hour < 5 THEN 1.0 ELSE 0 END)
    FROM fct_views WHERE local_date BETWEEN '2026-10-01' AND '2026-10-31'
  answer_type: scalar           # scalar | table | ordered_table | refusal
  tolerance: 0.005
  expected_metrics: [late_night_share]
```

**Categories (aim for 60–100 questions, ≥ 6 per category):**
`simple_agg`, `time_window`, `relative_date`, `metric_definition`, `comparison`, `ranking`, `session_derived`, `multi_step`, `ambiguous`, `unanswerable` (should refuse), `trap` (looks answerable but needs a caveat, e.g. "exact watch time").

Gold answers are computed by running `gold_sql` against the **synthetic dataset at a fixed seed**, so they're reproducible in CI. A check fails if any gold SQL errors or returns empty when it shouldn't.

### 7.2 Scoring (deterministic first)

| Score | How |
|---|---|
| `execution_accuracy` | the agent's final SQL result matches the gold result: scalars within tolerance, tables compared as sets (or ordered for `ordered_table`), column names ignored |
| `answer_accuracy` | the number(s) in the final text match the gold result (extracted with regex, then compared within tolerance), which catches correct SQL that's misreported |
| `metric_selection` | `metrics_used` ⊇ `expected_metrics` |
| `refusal_correct` | for `unanswerable`: refused or caveated, with no fabricated number |
| `caveat_present` | for estimate metrics: the answer mentions it's an estimate |
| efficiency | steps, tool calls, tokens, latency, cost |

An optional LLM-as-judge scores answer clarity (1–5) as a **secondary** metric only. It never decides correctness.

### 7.3 Bad-case taxonomy
Every failed question gets one primary label: auto-suggested from the trace, then confirmed by hand in `eval/labels/<run_id>.yaml`.

`wrong_metric_definition` · `wrong_time_window` · `timezone_or_day_boundary` · `hallucinated_table_or_column` · `join_or_grain_error` · `aggregation_error` · `sql_error_unrecovered` · `answer_misreport` · `missing_caveat` · `failed_refusal` · `over_refusal` · `tool_loop_or_step_limit`

### 7.4 Runs and reports
`scrollback eval run --config experiments/e03_rag.yaml --repeat 3`
- Writes `eval/runs/<timestamp>_<name>/results.jsonl` and `summary.json`.
- `scrollback eval report <run_a> <run_b>` produces markdown with overall and per-category accuracy, the bad-case distribution, the questions that regressed or improved, and cost and latency.
- Each config is run with `--repeat 3`, and the mean ± spread is reported, because LLM output varies even at temperature 0.

### 7.5 Planned experiment ladder
Each step is one config. Results go into the README table.

| ID | Change | Hypothesis |
|---|---|---|
| E0 | schema only, `run_sql` only | baseline |
| E1 | + table and column descriptions | fewer hallucinated columns |
| E2 | + metrics glossary via RAG | fewer wrong metric definitions |
| E3 | + few-shot examples via RAG | better time-window and session SQL |
| E4 | + SQL error self-correction | fewer unrecovered SQL errors |
| E5 | + `query_metric` semantic-layer tool | biggest jump on metric questions, fewer tokens |
| E6 | + "cannot answer" doc + refusal rules | fewer failed refusals, check over-refusal |
| E7 | model swap (small vs large) | the cost/accuracy tradeoff |

---

## 8. Interfaces

### CLI (`scrollback`)
`ingest`, `synth`, `build` (dbt), `ask "<question>" [--show-sql] [--chart]`, `eval run|report`, `kb index`, `alerts check`, `digest send`, `serve api`, `serve bot`.

### HTTP API (FastAPI) *(stretch)*
- `POST /ask {question} → {answer, sql[], metrics[], assumptions[], chart_url?, trace_id}`
- `GET /metrics` (the glossary) · `GET /health` · `GET /charts/{id}.png`
- Local only by default, with a bearer token from env.

### Telegram bot
- Free-text message → agent → reply with the answer, chart as a photo, and an inline "Show SQL" button.
- Commands: `/today`, `/week`, `/wrapped`, `/metrics`, `/feedback <good|bad> [note]`, which attaches feedback to the trace and feeds bad cases.
- *(stretch)* Scheduled jobs: a daily anomaly check and the Monday digest.

### Demo script (3 minutes)
1. Show the raw export: just timestamps and links, no durations. That's the problem.
2. Explain how watch time is estimated (§4.3), using one diagram of gaps between views.
3. Ask the bot a question live and show the answer, the SQL, the metric used and the caveat.
4. Ask something it can't answer ("which creator do I watch most?") and show the clean refusal.
5. Show the eval table: E0 → best config, plus the top bad-case types and how each was fixed.

---

## 9. Monitoring and alerts *(stretch)*

### Anomaly detection (`alerts check`)
- For each of `est_watch_minutes`, `views`, `sessions` and `late_night_share` on yesterday's date:
  baseline = median and MAD over the trailing 28 days, with a robust z = (x − median) / (1.4826·MAD).
- Alert if |z| ≥ 3 and the absolute change is above a floor (e.g. 20 min), to avoid noise on low-usage days.
- Also alert on data problems: no new data for N days after an expected export, rejects > 1% of the last ingest, or a failed dbt test.
- Message: the metric, yesterday's value, the baseline, the multiple, the top contributing sessions, and a "why?" button that runs the agent with a templated question.

### Weekly digest: "Scrollback Wrapped"
Covers total estimated watch time vs the previous week, active days, longest session, late-night share, peak hour, rewatch rate, top search terms, and one chart. The numbers are computed by deterministic SQL; the LLM only writes a 2-sentence narrative on top.

---

## 10. Optional enrichment *(stretch)*
`scrollback enrich --limit 500` calls TikTok's public oEmbed endpoint for unseen video IDs to get the title and author name, and writes them to `dim_video`.
- Rate-limited, cached and resumable. Failures are recorded rather than retried forever.
- Respect the terms of service. This is opt-in and off by default.
- It unlocks "top creators" questions. Eval questions for creators stay under `unanswerable` unless enrichment is on in that run's config.

---

## 11. Repository layout

```
scrollback/
├── README.md
├── CLAUDE.md
├── pyproject.toml
├── config.yaml
├── .env.example
├── .github/workflows/ci.yml
├── docs/
│   ├── DESIGN.md
│   └── ROADMAP.md
├── data/
│   ├── sample/              # synthetic export (committed)
│   ├── raw/                 # real exports (git-ignored)
│   └── warehouse/           # scrollback.duckdb (git-ignored)
├── dbt/
│   ├── dbt_project.yml
│   ├── profiles.yml
│   └── models/{staging,intermediate,marts}/ + schema.yml
├── knowledge/
│   ├── metrics.yml
│   ├── examples.yml
│   └── docs/*.md
├── src/scrollback/
│   ├── cli.py
│   ├── config.py
│   ├── ingest/{detect.py, parse.py, load.py}
│   ├── synth/generator.py
│   ├── warehouse/{db.py, dbt_runner.py}
│   ├── kb/{index.py, retrieve.py}
│   ├── semantic/compile.py          # metrics.yml → SQL
│   ├── agent/{loop.py, tools.py, sql_guard.py, llm.py, prompts/}
│   ├── charts.py
│   ├── tracing.py
│   ├── monitor/{anomaly.py, digest.py}
│   ├── api/app.py
│   └── bot/telegram_bot.py
├── eval/
│   ├── questions.yaml
│   ├── experiments/*.yaml
│   ├── runs/                # git-ignored except selected summaries
│   ├── labels/
│   └── harness/{run.py, score.py, report.py}
└── tests/
    └── fixtures/            # static test inputs (synthetic only)
```

---

## 12. Testing strategy
- **Unit:** format detection per variant, record parsing, video ID extraction, hashing and idempotent load, session and watch-time logic on small hand-built fixtures with expected values worked out by hand, SQL guard (blocks DDL/DML, multiple statements, non-mart tables), semantic compiler output, anomaly z-score.
- **dbt tests:** see §4.4.
- **Agent:** tool contract tests with a mocked `LLMClient` (scripted tool calls), so the loop, retries and step limit are tested without network calls.
- **Eval smoke test in CI:** a 10-question subset with a mocked or cheapest model, optional and behind a secret.
- **CI (GitHub Actions):** ruff, mypy, pytest, a check that regenerating `data/sample/` gives identical files, `synth` → `ingest` (run twice) → `dbt build`, and the eval gold-SQL validity check.

---

## 13. Key decisions and tradeoffs

| Decision | Alternative | Why |
|---|---|---|
| Build the agent loop by hand | LangChain/LangGraph | full visibility for bad-case analysis; a LangGraph port is a roadmap stretch item |
| DuckDB for both warehouse and vectors | Postgres + pgvector, Chroma | zero infra, one file, fast |
| dbt for transforms | plain SQL scripts | layered models, tests and docs are what a real data platform uses |
| Semantic-layer tool alongside raw SQL | raw SQL only | consistent metric logic and a measurable accuracy gain (E5) |
| Synthetic data for evals | evals on real data | reproducible, shareable, safe for CI |
| Deterministic scoring first | LLM-as-judge | correctness can't depend on another model's opinion |

## 14. Open questions
- Is the export timestamp actually UTC? Verify against a real export.
- Which export variants does the parser need to support? Collect one real export early.
- Should `is_rewatch` count a video as a rewatch on any repeat, or only after a gap?
- What should the default binge threshold and session gap be? Tune them by looking at the distribution of your real gaps.
