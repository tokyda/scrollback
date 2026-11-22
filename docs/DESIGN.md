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
| Embeddings | fastembed (`BAAI/bge-small-en-v1.5`, ONNX), local *(changed from sentence-transformers, owner decision 2026-11-20: no torch dependency)* | free, offline, private |
| Vector store | a separate DuckDB file (`paths.kb_index`, git-ignored); ~40 chunks are scored in memory with numpy *(changed from `array_cosine_similarity` in the warehouse: one warehouse per eval anchor, and the agent's warehouse connection is read-only)* | one store, no extra infra |
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
| Likes | `Activity › Like List › ItemFavoriteList`, `Your Activity › Like List › ItemFavoriteList`, or (v3) `Likes and Favorites › Like List › ItemFavoriteList` | `Date`/`date`, `Link`/`link` |
| Favourites | `… › Favorite Videos › FavoriteVideoList`; (v3) under `Likes and Favorites` | `Date`, `Link` |
| Searches | `… › Search History › SearchList`; (v3) `Your Activity › Searches › SearchList` | `Date`, `SearchTerm` |
| Shares | `… › Share History › ShareHistoryList` | `Date`, `Link`, `Method`, `SharedContent` |

- **Date format** is `YYYY-MM-DD HH:MM:SS`, assumed to be UTC. *Validate this:* check a few views against times you remember. The source timezone is a config value (`SOURCE_TZ`), not hard-coded. The display timezone is a separate config value (`LOCAL_TZ`, default `Asia/Singapore`), used for `local_date`, `local_hour` and so on.
- **Video ID** is extracted from the link with the regex `/video/(\d+)`.
- **Coverage:** the export may only hold a limited history window. Repeated exports overlap, so ingestion must be incremental and idempotent.
- **Input:** a JSON export, either a `.json` file or a `.zip` containing exactly one `.json`. The TXT export is out of scope. *(Changed: §3 previously said JSON/TXT.)*

**Variant detection** (`ingest/detect.py`) is declarative: each variant names an activity root key (`Activity` → `v1_activity`, `Your Activity` → `v2_your_activity`), optional top-level marker keys (`Your Activity` + `Likes and Favorites` → `v3_likes_favorites`) and a full path from the top of the document to each dataset's list, so a dataset may sit outside the activity root. When several variants match, the one with the most markers wins. *(v3 and full paths added 2026-11-22, after the first real export.)* Record keys match known fields case-insensitively, ignoring spaces and underscores. Detection fails loudly, naming the keys it found and never values, when:
- no known root, or more than one, is present at the top level;
- the root has no watch history section;
- a known section is missing its list key, or the list isn't a list of objects;
- no record in a non-empty list has a required field;
- a dataset is absent at its expected path but its list key (e.g. `ItemFavoriteList`) exists elsewhere in the document. *(Added 2026-11-22: under the v2 rules the first real export loaded without its likes, favourites and searches, silently, because absent sections are allowed.)*

**Unknown extra fields are tolerated.** A record key that doesn't map to a known field for its dataset is logged as a warning (key names only, once per section), stored in the record's `extras` JSON column under its original name, and ingestion continues. Extras never affect `record_hash`, so a later export that adds a field doesn't create duplicates. The CLI prints the warning and `ingest_log.dataset_counts` records the unknown key names per dataset, so a new field gets noticed and can be mapped properly. *(Changed: unknown fields used to fail the whole ingest. A real export that adds a harmless field such as a duration shouldn't block loading; the structural checks above still fail loudly.)*

Missing optional sections (likes, favourites, searches, shares) are allowed and recorded as absent. Unrelated sections such as profile or DMs are ignored. A `null` section or list is treated as empty. An individual record that is missing a field is rejected (§4.2), not treated as a shape error.

#### Format assumptions to verify against a real export
Checked against the first real export on 2026-11-22 (v3 layout; all five datasets present, 0 rejects, no unknown fields). Results in **bold**. *(Exact record counts and history dates from the real export were replaced with shares and plain wording on 2026-11-22 (Phase 7), so the public repo holds no figures about the owner's data.)*

1. The export is a single JSON object, delivered as `user_data_tiktok.json` or as a zip containing it. **Held** (a `.json` file).
2. The activity root is `Activity` or `Your Activity`, and exactly one of them is present. **Held** (`Your Activity`).
3. The watch history path is `Video Browsing History › VideoList` (v1) or `Watch History › VideoList` (v2). **Held** (v2 path).
4. Likes, favourites, searches and shares sit at the same section/list names in both variants (`Like List › ItemFavoriteList`, `Favorite Videos › FavoriteVideoList`, `Search History › SearchList`, `Share History › ShareHistoryList`). **Broke:** likes and favourites moved to a top-level `Likes and Favorites` object (each section also has an `App` integer beside the list), and searches to `Your Activity › Searches › SearchList`. Shares held. Added as `v3_likes_favorites`.
5. Record field names are `Date`, `Link`, `SearchTerm`, `Method`, `SharedContent`, with casing that may vary (e.g. `date`/`link` in likes). Any other field is kept in `extras` with a warning. Check the first real ingest for such warnings: a field like a watch duration would change the watch-time method in §4.3. **Held:** watch `Date`/`Link`, likes `date`/`link`, favourites `Date`/`Link`, searches `Date`/`SearchTerm`, shares `Date`/`Link`/`Method`/`SharedContent`. No extra fields, so no duration field: §4.3 stands.
6. `Date` is always `YYYY-MM-DD HH:MM:SS`, with no timezone suffix, and the time is UTC. **Held, resolved 2026-11-22:** the format holds for every record in all five lists, and the owner confirmed UTC. Five recent session starts converted UTC → Singapore matched when they were watching, and the hour-of-day profile has its trough at 05:00–08:00 Singapore time. `SOURCE_TZ` stays `UTC`.
7. Watch, like and favourite links contain `/video/<digits>`, either as `tiktokv.com/share/video/<id>/` or as `tiktok.com/@<user>/video/<id>`. **Held**, all in the `tiktokv.com/share/video/<id>/` form.
8. Share records may point at non-video content (a profile, a live stream, a sound), in which case `Link` has no video ID and `SharedContent` says what was shared. **Held:** almost all shares are `share_video`; the handful of `share_live` and `share_profile` records have no video ID.
9. An empty history appears as `[]`, `null` or a missing section, and none of these means a broken export. **Held where seen:** empty lists elsewhere in the export are `null`; none of the five datasets was empty.
10. Record order is unknown (newest first is assumed in the synthetic data); the parser doesn't rely on it. **Newest first** in watch history (no out-of-order pair).
11. The file is UTF-8, possibly with a BOM. **Held**, no BOM.
12. The export itself may contain exact duplicate records. **Held for shares only:** roughly two in five share records are exact duplicates (probably one video sent to several chats; there is no recipient field), plus a pair that differs only in `Method`; all of them dedupe by `record_hash`. Watch, likes, favourites and searches have none.
13. How far back the export reaches (the history window) is unknown. **Differs by dataset:** watch history, searches and shares cover about the last 6 months; likes reach back well over a year (a round count, so probably capped) and favourites several years.
14. A second-resolution timestamp plus video ID uniquely identifies a view; two genuine views of the same video in the same second don't occur. **Held:** no (second, video) collisions. Some seconds do hold views of different videos, which is fine.

### 4.2 Ingestion
`scrollback ingest <path-to-export.json>`

1. Detect the format variant, then extract each dataset into a list of normalised dicts.
2. Validate each record with pydantic: date parses, link present, video ID extractable. Rejected records go to `raw_rejects` with a reason (`missing_date`, `invalid_date`, `missing_link`, `no_video_id`, `missing_search_term`). The link and video ID are required for watch, like and favourite records. For shares they're optional (`video_id` is null for non-video shares).
3. Compute `record_hash = sha256(dataset|timestamp|key)`. `timestamp` is the parsed export timestamp reformatted as `YYYY-MM-DD HH:MM:SS` (no timezone conversion, so changing `SOURCE_TZ` never changes a hash). `key` is the video ID for watch/like/favourite, the normalised search term (NFKC, case-folded, whitespace collapsed) for searches, and the video ID, else the link, else `SharedContent` for shares. *(Changed from hashing the raw date and link strings: the same event can appear with different link shapes across variants, e.g. `tiktokv.com/share/video/<id>` vs `tiktok.com/@user/video/<id>`, and must deduplicate.)*
4. Insert into `raw_*` with `ON CONFLICT DO NOTHING` on `record_hash`, so the step is idempotent. Rejects are keyed by `reject_hash = sha256(dataset|reason|canonical JSON payload)` with the same rule, so re-ingesting a file doesn't duplicate its rejects. *(Added: `raw_rejects` previously had no key.)* Steps 4–5 run in one transaction; shape errors (§4.1) are raised before anything is written.
5. Write one row to `ingest_log` **on every run**: file hash, variant, rows per dataset, how many were new vs duplicate vs rejected. "Idempotent" means `raw_*` and `raw_rejects` don't change on a re-run; the log still records the attempt, with `rows_new = 0`. *(Clarified: the roadmap's "running it twice changes nothing" refers to the data tables, not the audit log.)* "Duplicate" counts valid records that weren't inserted, either because they repeat within the file or because they're already loaded.
6. Run `dbt build` (the same step as `scrollback build`). `--no-build` skips it. If any model or data test fails, `ingest` exits 1, names the failing nodes, and says that the raw data was still loaded, so fixing the problem only needs `scrollback build`. Building a warehouse with no watch history fails early with a pointer to `ingest`.

### 4.3 Core metric problem: there is no watch duration
The export records **when a view started**, not how long it lasted, so watch time has to be estimated. The method is versioned in `metrics.yml` and parameterised by dbt vars. The parameter values live in `config.yaml` (`metrics:`) as the single source of truth, and `dbt_runner` passes them (plus `source_tz` and `local_tz`) to dbt as `--vars`. `dbt_project.yml` has **no** var defaults, so running dbt directly without `--vars` fails loudly instead of using stale values. *(Changed: fallback defaults in `dbt_project.yml` were a second source of truth.)* dbt runs in a subprocess, because dbt-duckdb keeps its DuckDB connection open for the life of the process.

- **Session:** an ordered run of views where the gap between consecutive views is ≤ `session_gap_min` (default **30 min**). A gap exactly equal to the threshold stays in the session. Views are ordered by UTC timestamp (so daylight-saving changes can't distort gaps), with ties in the same second broken by `view_id`.
- **Per-view estimated watch seconds:**
  - not the last view in its session: `min(gap_to_next_view_sec, view_cap_sec)`, with `view_cap_sec` default **180** (synthetic data and evals; **120** for the real export, see below)
  - last view in its session: the median of the other estimates in that session, falling back to `default_last_view_sec` (default **20**; 10 for the real export) for a single-view session. For an even number of other estimates the median is the mean of the middle two, so `est_watch_sec` is a DOUBLE and can end in .5.
  - config validation requires `default_last_view_sec ≤ view_cap_sec`, so every estimate lies in [0, `view_cap_sec`].
- **Caveats, which the agent must surface when relevant:** pauses within the cap are counted as watching, long videos are undercounted at the cap, and background play is invisible.

**Parameters for the real export** *(owner decisions 2026-11-22, first real export, §4.1)*. `config.yaml` holds the values for the owner's data. The synthetic data, tests and evals don't read them: they pin their own in `eval/questions.yaml` (`params`: gap 30 min, cap **180 s**, last view 20 s, binge 60 min), so the eval gold and the test warehouses never move when the real config is tuned.

| Parameter | Synthetic / eval (pinned) | Real (`config.yaml`) | Why |
|---|---|---|---|
| `session_gap_min` | 30 | 30 | `inspect gaps` shows no natural break: gaps thin out smoothly past 20 min, so the conventional 30 stays. |
| `view_cap_sec` | 180 | **120** | Calibrated against phone Screen Time (below): 180 s overshoots it. |
| `default_last_view_sec` | 20 | 10 | Close to the real median within-session gap. It only affects single-view sessions (under 5% of sessions). |
| `binge_threshold_min` | 60 | 60 | Most real sessions are well under an hour; 60 flags roughly the longest sixth of sessions. |

**Calibrating the cap against Screen Time** *(2026-11-22)*.
- **Method:**
  - The owner's phone Screen Time for TikTok over one week in November 2026. TikTok is only on that phone, so Screen Time covers all their TikTok use.
  - Compared on the four Singapore calendar days that are complete in both sources. A fifth day, complete in both but near the end of the export, was checked separately.
  - Estimated watch minutes per day were computed with session gap 30 min and last view 10 s, for caps of 120, 180, 240 and 300 s. The offline calculation matched the warehouse's own per-day totals exactly at the old settings.
- **Result**, estimate vs Screen Time over the four days: cap 120 s **−8.2%**, 180 s **+4.7%**, 240 s +14.3%, 300 s +22.2%. At 180 s the estimate was above Screen Time on 3 of the 4 days; at 120 s it was below on 3 of 4, and the fourth was within the reading error of the bar chart. The fifth day behaved the same way (−9% at 120 s, +7% at 180 s), so it isn't cut off by the export.
- **Why 120 s.** Screen Time counts all foreground time in the app (comments, DMs, search, LIVE, browsing), so a watch-time estimate should come out **somewhat below** it, not equal to it. 180 s overshoots, which means gaps that include time away from the screen or idle time were being credited as watching. 120 s is the round value below it; it wasn't fitted to minimise the error, since one week is a small sample. Screen Time is an upper bound, not a target.
- **Cost:** a gap over 2 minutes now counts as 2 minutes, so videos longer than that are undercounted. About 8% of the real within-session gaps are capped, and total estimated watch time over the whole history is about 13% lower than at 180 s.
- The owner's per-day Screen Time figures, and the real data's counts, are not stored in the repo.

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

**staging** (typed and timezone-converted views, one model per raw table; raw rows are already unique by `record_hash`, which becomes the row key)
- `stg_watch(view_id, video_id, event_ts_utc, event_ts_local, local_date, local_hour, dow)`: `dow` is ISO, 1 = Monday … 7 = Sunday
- `stg_like(like_id, video_id, event_ts_utc, event_ts_local, local_date)`, `stg_favorite(favorite_id, …same)`
- `stg_search(search_id, search_term, search_term_norm, event_ts_utc, event_ts_local, local_date)`: `search_term_norm` is NFC, lower-case, trimmed, single-spaced
- `stg_share(share_id, video_id, method, shared_content, event_ts_utc, event_ts_local, local_date)`
*(Added `stg_favorite` and `stg_share`; they have no marts yet because no metric uses them.)*

**intermediate**
- `int_view_gaps`: each view joined to the next view's timestamp, plus `gap_sec`
- `int_sessions`: session assignment via a running sum of "new session" flags. `session_id` is the `view_id` of the session's first view, so it's stable across rebuilds.

**marts** (the only layer the agent may query)
- `fct_views(view_id, video_id, session_id, event_ts_utc, event_ts_local, local_date, local_hour, dow, is_weekend, is_late_night, gap_to_next_sec, view_seq_in_session, is_last_in_session, est_watch_sec, is_rewatch)`. `is_rewatch` is true when the same video was viewed **earlier in the loaded history**, at any distance; views from before the export window are invisible. *(Resolves the §14 open question.)* `is_late_night` is `local_hour` in [`late_night_start_hour`, `late_night_end_hour`).
- `fct_sessions(session_id, start_ts_local, end_ts_local, local_date, n_views, est_watch_sec, is_binge)`. `local_date` is the session's **start** date. `end_ts_local` is the **observed** start of the last view, not an estimated end. `is_binge` is `est_watch_sec ≥ binge_threshold_min × 60`.
- `fct_likes(like_id, video_id, event_ts_local, local_date, liked_after_watch BOOL)`. `liked_after_watch` is true when a view of the same video happened at or before the like and at most `session_gap_min` minutes earlier.
- `fct_searches(search_id, search_term_norm, event_ts_local, local_date)`
- `dim_date(local_date, dow, dow_name, is_weekend, iso_week, week_start, month, month_start, year)`: a contiguous spine over the **watch-history window**, from the first to the last date with a view. Days outside it are unknown, not zero: before the window the export has no watch history, and after it the export hasn't happened yet. Likes and searches outside the window stay in `fct_likes` and `fct_searches`. *(Changed 2026-11-22, owner decision: the spine used to start at the earliest event of any dataset. In the first real export the likes reach back more than a year before the watch history, so that whole stretch looked like zero-view days. Eval gold is unchanged.)* `iso_week` is a label like `2026-W47`, and `week_start` is its Monday.
- `agg_daily(local_date, dow, is_weekend, views, est_watch_min, sessions, longest_session_min, late_night_views, likes, searches, is_active, active_days_7d)`: built from `dim_date` with left joins, so missing days are 0, not absent. View-level columns use each view's own date; `sessions` and `longest_session_min` use the session's start date. `active_days_7d` counts active days in the 7 days ending on that date.
- The day boundary is **local midnight** in `LOCAL_TZ`.
- `dim_video(video_id, title, author_name, enriched_at)`: optional, filled by enrichment (Phase 8)

**dbt tests**
- `unique` / `not_null` on keys
- `accepted_range` (a generic test in `dbt/macros/`, so there's no `dbt_utils` dependency): `est_watch_sec` between 0 and `view_cap_sec`, `local_hour` between 0 and 23, `active_days_7d` between 0 and 7
- relationships: `fct_views.session_id → fct_sessions`
- custom: no `local_date` in the future; `agg_daily` row count equals the `dim_date` span; daily views don't exceed `max_daily_views` (3,000); session totals equal the sum of their views
- freshness: a singular test with `severity: warn` that fires when the latest event is more than `freshness_warn_days` (14) days old. *(Changed from dbt source freshness, which `dbt build` doesn't run.)*

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
- **What the LLM sees:** query results are sent to the LLM provider. Config `AGENT_RESULT_MODE=aggregate_only` (the default, used for synthetic and real data alike so evals measure the same agent) makes the SQL guard reject row-level results:
  - the outermost query (each branch of a UNION) must aggregate, with an aggregate function outside a window or a `GROUP BY`, **or** read only sources that are already aggregated: `agg_*`/`dim_*` tables, or subqueries and CTEs that aggregate;
  - `GROUP BY` on a bare row ID or raw timestamp (`view_id`, `like_id`, `search_id`, `share_id`, `favorite_id`, `session_id`, `event_ts_*`, `start_ts_local`, `end_ts_local`) is rejected; grouping by a derived value such as `date_trunc('hour', event_ts_local)` is allowed;
  - `video_id` and search terms **may** appear in results (owner's decision, 2026-11-20), e.g. "most rewatched video" or "top searches";
  - `describe_table` shows sample rows only for `agg_*`/`dim_*` and column statistics (min, max, distinct count) for `fct_*`.
  This guards against accidental row dumps. It isn't a security boundary against a determined model; the execution layer in §6.3 is the second line.
- The Telegram bot only answers chat IDs listed in `TELEGRAM_ALLOWED_CHAT_IDS` and ignores all others silently. It refuses to start unless `aggregate_only` is on. Its logs hold no question text, answers, results, chat IDs or token (§8).
- Bot feedback and the message map live under `traces/` (git-ignored). Candidate eval questions from bad feedback go to `eval/candidates/` (git-ignored), because they hold real questions and answers (§8).

---

## 5. Metrics layer: `knowledge/metrics.yml`

The single source of truth for metric definitions. It's used in three places:
1. RAG documents for the agent
2. the `query_metric` tool, which compiles the definition to SQL
3. the generated docs page

**Schema** (validated by pydantic in `src/scrollback/semantic/metrics.py`) *(changed: adds `type`, `unit`, `params` and a shared caveat vocabulary)*:
- top level: `version`; `watch_time_method {name, version, doc, summary}`, which versions the §4.3 estimation; `caveats {key: text}`, which defines every caveat key once; and `metrics`.
- per metric: `name`, `label`, `description`, `type`, `sql`, `table` (marts only), `time_column`, `default_grain` (day/week/month), `unit` (count/minutes/ratio/days), `params` (config parameters the metric depends on), `caveats` (keys from the top-level map), `synonyms`.
- **`type: simple`**: `sql` is a single aggregate expression over `table`. It's compiled as `SELECT <sql> FROM <table> WHERE <time_column> BETWEEN $start AND $end`, optionally grouped by `time_column` for a per-day series.
- **`type: query`**: `sql` is a complete query returning one `value`, using the named parameters `$start` and `$end`. This is for metrics that aren't one aggregate over one table (`streak_days`, a gaps-and-islands query). There's no per-day series.
- Cross-table and windowed metrics are made simple through `agg_daily`: `like_rate` is `SUM(likes) * 1.0 / NULLIF(SUM(views), 0)` on `agg_daily`, and `active_days_7d` is a precomputed `agg_daily` column read with `ARG_MAX(active_days_7d, local_date)`, which gives the value on the last day of the range.
- Text fields can contain `{param}` placeholders (e.g. `{view_cap_sec}`). They're filled from `config.yaml` at indexing time, so definitions always match the tuned values. A test fails on unknown placeholders, caveats or params.
- Tests run every metric on the synthetic warehouse (whole range and per day), reconcile each with the marts, and check all 15 against hand-computed values on the §12 fixture.

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
| `active_days_7d` | active days in the trailing 7-day window ending on the range's last day (the single-user analogue of DAU/WAU) |
| `sessions` | count of sessions |
| `avg_session_minutes` | mean session est_watch_sec ÷ 60 |
| `binge_sessions` | sessions with est watch ≥ `binge_threshold_min` (default 60) |
| `late_night_share` | views with local_hour in [0, 5) ÷ views |
| `weekend_share` | weekend views ÷ views |
| `likes`, `like_rate` | likes; likes ÷ views in the same period (NULL when there are no views) |
| `searches` | count of searches |
| `streak_days` | longest run of consecutive active days inside the range (`type: query`) |

**Glossary docs** (`knowledge/docs/*.md`) cover concepts that aren't single metrics: the watch time estimation method, the timezone and day-boundary rule, the definition of a session, what the data **cannot** answer (true durations, creators without enrichment, video content, ads), and table and column descriptions (generated from dbt `schema.yml`).

**Few-shot examples** (`knowledge/examples.yml`) are verified question → SQL pairs. They must never overlap with eval questions; a CI check enforces this with normalised-text and embedding similarity.

---

## 6. Agent

### 6.1 Loop
A plain tool-calling loop. No framework, so every step is visible and debuggable.

```
system prompt (role, tools, table list, rules)          byte-stable, so cacheable
user message (today + timezone, question, chart request?)
→ LLM ⇄ tools (max N steps, default 8)
→ final_answer(text, result_ids[], metrics_used[], assumptions[], chart_id)
```

Implemented in `src/scrollback/agent/loop.py` on the Messages API (manual loop; see §13).
- **Today and timezone go in the user message, not the system prompt.** *(Changed: a date in the system prompt changes daily and breaks prompt caching.)*
- **Tool choice is `auto`.** Current models reject forced tool use, so the prompt says to finish with `final_answer` and its schema is strict. A turn with no tool call gets one reminder; a second one ends the run as `no_final_answer`.
- **One step is one LLM call.** All tool calls in a turn run (with `final_answer` last) and their results go back in a single user message.
- **Statuses:** `answered`, `step_limit`, `refused`, `no_final_answer`, `max_tokens`, `error`. Only `answered` carries an answer; the others never produce a number.
- **`final_answer` cites `result_ids`, not SQL.** The loop fills in `sql_used` from the queries that actually ran (after the guard's rewrite), so the SQL shown to the user is always the SQL that produced the numbers (U4). `chart_id` refers to a `make_chart` result. *(Changed from `sql_used[]` / `chart_path?`, which the model could misreport.)*

### 6.2 Tools

| Tool | Input | Output | Notes |
|---|---|---|---|
| `search_knowledge` | `query` | top-k chunks (metrics, docs, table descriptions; examples from E3) | hybrid: embedding similarity + keyword match on synonyms. *As built: k is `llm.retrieval_k` (6), a config value, not a model choice, so experiments control it* |
| `describe_table` | `table` | columns, types, descriptions, 3 sample rows | marts only |
| `query_metric` | `metric`, `grain`, `start`, `end`, `filters`, `group_by` | rows + compiled SQL | compiles from `metrics.yml`; the preferred path |
| `run_sql` | `sql` | rows (≤ 200), column names, row count, or error | read-only guard below |
| `make_chart` | `result_id`, `kind` (line/bar), `x`, `y`, `title` | `chart_id` (PNG under `charts/`) | charts a real `run_sql` result *(changed from model-typed `rows`: cheaper, and the model can't chart invented numbers)* |
| `final_answer` | `text`, `result_ids[]`, `metrics_used[]`, `assumptions[]`, `chart_id` | — | ends the loop; unknown ids are returned as an error to fix |

**v0 (E0 baseline)** has only `describe_table`, `run_sql`, `make_chart` and `final_answer`. All four use strict JSON schemas and are validated again in code. `search_knowledge` arrives in Phase 5; `query_metric` is a stretch item (§7.5).
- `describe_table` returns columns, types and a row count. Column and table descriptions are behind `agent.describe_include_descriptions` (off in E0, on from E1). In `aggregate_only` mode it shows sample rows only for `agg_*`/`dim_*`, and column statistics for `fct_*` (§4.6).
- `run_sql` results carry a `result_id` (`r1`, `r2`, …). Errors come back as `[rule_code] message` with `is_error`.
- **`search_knowledge` (as built, Phase 5).** Off unless `agent.search_knowledge` is on, and then it is listed first. Chunks (`kb/chunks.py`):
  - one per metric in `metrics.yml` (definition, SQL, caveat text, synonyms);
  - one per `##` section of each glossary doc, plus its intro when it has one;
  - one per mart from `_marts.yml`;
  - examples from `knowledge/examples.yml` (E3).

  `agent.kb_sources` picks which sources a run may see. E2a uses `[metrics, docs, tables]`; E3 adds `examples`; E6 adds `cannot_answer`, the sections of `cannot_answer.md`. Retrieval (`kb/retrieve.py`) fuses two rankings with reciprocal-rank fusion (`kb.rrf_k` = 60):
  - cosine similarity of bge-small embeddings, with BGE's query instruction on the query;
  - BM25 over title, text and keywords. Keywords count three times, and identifiers are split on `_`. A chunk's keywords are its own names: a metric's name, label and synonyms; a table's name and column names; a glossary section's heading. *(Changed 2026-11-21: glossary sections also took every backticked identifier in their body as a keyword, so a section naming many metrics matched almost any query. Measured on dev with E2a's sources, recall@6 stayed 79.3% (40 → 39 questions with everything found; q074 gained the caveats section, q093 lost it), and the "sessions that cross midnight" section, the one crowding results, appeared in 28 top-6 lists where it isn't expected, down from 29. The crowding came mainly from the year in its worked example's dates matching dates in questions.)* **Pure numbers are not keyword tokens** (owner decision 2026-11-21): a chunk's numbers are example values, not vocabulary. Measured the same way: recall@6 79.3% → 81.6% (42/58 fully found; q071, q074 and q075 gain the watch-time caveats section, no losses; the categories at 100% stay there), and the crowding section's unexpected top-6 appearances 28 → 23.

  A chunk that matches no query token and isn't in the embedding top 3 is dropped. Chunk text keeps `{param}` placeholders, which are filled from the run's settings when returned. The index stores a hash of the chunk texts and the model, and it is rebuilt when either changes (`scrollback kb index`, `kb search`). Tests and CI use a deterministic offline hashing embedder (`kb.embedding_model: hashing`).
- **Prompt and tool must agree.** Loading a prompt that mentions `search_knowledge` while the tool is off, or the reverse, fails (`PromptToolMismatchError`).

### 6.3 SQL guard (`run_sql`)
- Parse with sqlglot (DuckDB dialect). Reject anything that isn't a single `SELECT`/`WITH`.
- Table allowlist: `fct_*`, `dim_*`, `agg_*`.
- Force `LIMIT 200` if none is given, plus a statement timeout.
- Read-only DuckDB connection (`read_only=True`).
- Errors are returned to the model as structured text so it can self-correct, with a maximum of 2 retries per query.

Implemented in `src/scrollback/agent/sql_guard.py`. Every rejection has a stable rule code, recorded in traces and covered by a unit test:

| Rule | Rejects |
|---|---|
| `parse_error` | SQL that doesn't parse (or is empty) |
| `multiple_statements` | more than one statement |
| `not_select` | anything other than SELECT / WITH … SELECT / UNION, INTERSECT, EXCEPT |
| `write_or_ddl` | a write, DDL, PRAGMA, SET, ATTACH, COPY, INSTALL/LOAD, DESCRIBE/SUMMARIZE or transaction anywhere in the tree |
| `table_function` | `FROM <function>(…)`, e.g. `read_csv`, `range`, `glob`, `duckdb_settings` |
| `schema_not_allowed` | a table qualified with a schema other than `main` (e.g. `information_schema`) |
| `table_not_allowed` | a table outside `fct_*`/`dim_*`/`agg_*` (names defined inside the query are fine), including file paths used as tables |
| `function_not_allowed` | functions that read files, the environment or settings (`getenv`, `read_*`, `current_setting`, …) |
| `not_aggregate` | *(aggregate_only)* row-level results, see §4.6 |
| `row_level_grouping` | *(aggregate_only)* `GROUP BY` on a row ID or raw timestamp column |

- **LIMIT:** `LIMIT max_rows` (200) is added when there's none, and a larger or non-literal LIMIT is reduced to it. *(Clarified: a larger limit used to pass unchanged.)*
- **Execution layer** (defence in depth behind the guard): a read-only connection opened with `enable_external_access=false` and `lock_configuration=true`, so file reads, `getenv` and `SET` fail even if the guard misses them. The statement timeout interrupts the connection after `statement_timeout_sec`.
- **Retries:** after `sql_max_retries` (2) consecutive failed `run_sql` calls, `run_sql` stops executing and tells the model to answer with what it has. A success resets the count. In the E0 baseline the error text is the rule code plus DuckDB's or the guard's message; E4 enriches it (§7.5).

### 6.4 System prompt rules (abridged)

**`system_v1.md` (E0) is deliberately minimal:** role, the four tools, the mart table names (no descriptions), "base every number on a run_sql result and cite its result_ids", the `aggregate_only` rule when it's on, relative dates resolved against the supplied today with Monday–Sunday weeks and the range stated in `assumptions`, ambiguity handling, "fix failing queries", and "lead with the number". It contains **no** metric definitions, glossary content, estimate caveats or refusal rules; those are what E1–E6 add and measure. The full target rule set is below.

**`system_v2.md` (E2b)** = `system_v1_search.md` + the lookup rule. **`system_v3.md` (E6a, E6b)** = v2 + three rules, appended to its Rules list: say when a number is an estimate and why in a few words; if the data can't answer, say so in the first sentence and name what's missing (no guessing; a related number the data has may follow, labelled); a day or period with no rows outside the data's first and last dates is unknown, not zero. Tests check each version is exactly its parent plus its rules.

- Always resolve metric definitions through `search_knowledge` before computing anything.
- Prefer `query_metric`, and fall back to `run_sql` for anything it can't express.
- Resolve relative dates ("last week") against the supplied `today` and timezone. State the resolved range in `assumptions`. Weeks run Monday to Sunday.
- If a metric is an estimate, say so.
- If the data can't answer the question, say what's missing. Don't guess.
- If the question is ambiguous, pick the most reasonable reading and state it. Don't ask questions back in single-shot mode.

### 6.5 Answer format
`scrollback ask "<question>" [--show-sql] [--chart] [--today YYYY-MM-DD]` prints the answer text, `Metrics:` (the model's `metrics_used`; in E0 these are free text with no definitions, which arrive with the metrics layer in E2), the assumptions, the SQL (with `--show-sql`, otherwise just a count), the chart path, and a trace line (`trace_id · status · steps · tool calls · tokens · cost · latency`). Any status other than `answered` prints why and exits 1. `--today` pins the date for evals and reproducible checks.

Target layout (once metric definitions are in the agent's reach):
```
You watched ~142 min last Sunday (15 Nov), your 3rd-highest day this month.

Metric: est_watch_minutes (estimated from gaps between views; pauses up to 3 min count as watching)
Range: 2026-11-15, Asia/Singapore
SQL: <collapsed>
[chart]
```

### 6.6 Tracing
Every run writes one JSON line to `traces/` and a row to `agent_traces` in DuckDB. Each record holds the question, model, prompt version, every tool call (name, args, duration, error), the final answer, tokens, cost estimate and latency. Traces are the raw material for bad-case analysis.

Implemented in `src/scrollback/tracing.py`:
- **JSONL:** `traces/YYYY-MM-DD.jsonl`, one line per run, written for every status including failures.
- **Each record:** `trace_id`, `today`, `local_tz`, the model requested and the models that served, `prompt_version`, `system_prompt_sha256` (the SHA-256 of the rendered system prompt), and a snapshot of the non-secret config (llm, agent, metric params, timezones). Per LLM call: step, stop reason, input/output/cache-read/cache-write tokens, estimated cost, latency, text, tool names and request ID. Per tool call: input, duration, error code, a capped copy of the output, and metadata (the executed SQL, `result_id`, row count, chart path). Plus the final answer with `sql_used`, status and total latency.
- **Cost** is estimated from a dated per-model price table (`agent/pricing.py`) applied to each call's usage and serving model; an unknown model gives `null`, not a guess.
- **Writing `agent_traces`:** the agent's read-only connection is closed before the row is written through a separate write connection. If the warehouse is locked, the JSONL line is still written and the CLI warns.
- **Privacy:** traces contain aggregated query results and live only in git-ignored places. They never contain secrets; a test plants a fake key and checks both stores.
- `agent_traces` isn't a mart, so the agent can't query it.
- *(Changed in Phase 5: `prompt_version` became text and `system_prompt_sha256` was added. A table written before that is migrated in place on the next write: the column type changes to VARCHAR and old rows keep their version, with no hash.)*

### 6.7 Config and versioning
- The prompt lives in `src/scrollback/agent/prompts/system_v{version}.md`, and the version is recorded in traces and eval runs. **One prompt version is one exact text** (owner decision 2026-11-21): `agent.prompt_version` is a string (`"1"`, `"1_search"`, `"2"`, `"3"`), a published file is never edited, and every trace and eval run's `config.json` records `system_prompt_sha256` of the rendered prompt, so two runs with the same version can be checked for identical prompts.
- Model, effort (default `medium`), max steps, retrieval k and tool on/off flags all come from `config.yaml` and env, so experiments are config diffs, not code diffs. *(Changed: there's no `temperature`. Current Claude models reject sampling parameters, and from API version 2026-10-21 every model does; `effort` controls depth and spend instead, and §7.4's `--repeat 3` covers run-to-run variance. A config that still sets `temperature` fails validation.)*
- Default model: `claude-sonnet-5-5` at `medium` effort for the E0 baseline. Comparing with `claude-opus-5-5` is E7.
- **Interactive config** *(added in Phase 6, owner decision 2026-11-22)*. `scrollback ask` and the Telegram bot run `interactive.experiment` from `config.yaml`, which defaults to the best eval config, `eval/experiments/e06b_cannot_answer_haiku.yaml` (Haiku E6b). Only the experiment's `llm`/`agent` overrides apply. The question set's pinned eval params do not, so interactive runs use the calibrated real-data metrics in `config.yaml` (§4.3). `ask --experiment <file>` picks another experiment, and `ask --no-experiment` (or `experiment: null`) uses `config.yaml`'s `llm`/`agent` as they are. Evals never read the `interactive` section: an eval run is still `config.yaml` + its experiment + the pinned params, and a test checks that every experiment's eval settings are identical with and without the interactive default. The shared loader lives in `src/scrollback/experiments.py`. *(Why: the CLI and the bot should behave identically and run what was measured, and changing the top-level `llm`/`agent` would have changed the base that every experiment builds on.)*

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

**As built** (`src/scrollback/evals/`, data in `eval/`):
- **Pinned parameters.** `questions.yaml` starts with a `params:` block (timezones and every metric parameter) used for both gold answers and eval runs. Tuning `config.yaml` on real data therefore never moves the gold answers. *(Added.)*
- **One warehouse per `today` anchor.** For anchor T, the committed sample is filtered to records whose local date is ≤ T, as if exported at the end of that day, then ingested and built with the pinned params. A question asked "on" T can't see later data, and "this month" on the 1st really is one day. Warehouses are cached in `data/warehouse/eval/`, keyed by the sample, anchor, params and dbt project. *(Added: a single full warehouse would let past anchors see "future" data.)*
- **Extra question fields:**
  - `split` (dev/test, stratified by category; test is held out for baseline and final runs);
  - `tests` (one line on what the question checks);
  - `units` (one per gold column, which drives tolerant matching);
  - `alternatives` (other acceptable readings, `ambiguous` only);
  - `wrong_values` (numbers that only come from a wrong definition, or wrong weekday labels for a date written `"2026-11-14 Friday"`; added for q097);
  - `requires_caveat` (true for every `trap`);
  - `likely_failure` (a hint for label suggestion);
  - `expect_empty`.
- **`answer_type: no_data`** *(added)*, `trap` only. The right answer is "there's no data for that" (e.g. "today" when the export ends yesterday). It passes only if the answer says so, and **any** stated amount, including 0, fails, because unknown isn't zero (§4.4 `dim_date` coverage).
- **Ambiguous-reading policy.** Alternatives exist only in the `ambiguous` category. When a review finds that a question elsewhere has more than one reasonable reading, it **moves to `ambiguous` with every reasonable reading accepted**, rather than being reworded after the fact (q056 "watch more": watch time 6.09× or videos 5.73×; q082 "last Monday" asked on a Friday). A reasonable reading found later is added as another alternative (q060 "at night": 10pm–6am; and, after the Haiku review on 2026-11-21, the night share of estimated *watch time* as well as of views, for the same four windows, accepted by the owner as a reading they would have taken before seeing answers). An ambiguous answer must match one reading in both its result and its text, and must state its assumptions.
- **Ties:** ranking and top-N golds were checked for ties at their cutoff (none at review, 2026-11-20). Ties below the cutoff don't matter, because a top-N answer is scored on its first N rows only. If a future gold has a tie at the cutoff, every tied answer must be listed as an alternative.
- **Round 2** *(added 2026-11-20, after E0 scored ~95%)*: 20 harder questions (q077–q096, 13 dev / 7 test), where no single column gives the answer away. They cover relative windows the marts don't encode (week before last, last full week, this quarter, same weekday last week, past fortnight), session statistics with no column (median, share by session start hour, sessions per *active* day, longest by estimate vs span), runs of inactive days, an imputed last view, "exactly" traps and two more no-proxy refusals. The set is now 96 questions (64 dev / 32 test).
- **Round 3** *(added 2026-11-22, from bot feedback)*: **q097** (dev, `time_window`, anchor 2026-11-20). It asks for watch time on each of the last 7 days in the data, with the weekday for each day. It was promoted from the bot's `/week` answer on real data, which labelled all seven days one weekday off. Its gold is written from the definitions (`agg_daily.est_watch_min` joined to `dim_date.dow_name`, relative to the data's last day). On the synthetic anchor the data ends on 19 Nov, so the window is 13-19 Nov and the agent has to find the last data day itself. Its `wrong_values` are the 14 off-by-one labels, one day back and one forward for each date. The set is now 97 questions (65 dev / 32 test). **No eval was re-run:** every reported result (README, `final_test.md`) is on the 96-question set, and q097 enters from the next dev run on.
- **Unanswerable questions have no reasonable proxy in the data.** No search terms or gaps can stand in for the answer, so a refusal is the only right answer.
- **Writing rules.** Gold SQL follows `knowledge/metrics.yml` and the glossary, and questions were written without running the agent on them.
- **`scrollback eval check-gold`** (runs in CI) validates the schema and its invariants: unique ids, ≥ 6 per category, both splits in every category, refusal ⇔ `unanswerable`, caveat ⇔ `trap`. It also runs every gold SQL and alternative and checks shapes, that `expected_metrics` exist, and that no wrong value equals its gold. **Every gold SQL and alternative must pass the agent's own `aggregate_only` SQL guard**, so no question needs SQL the agent isn't allowed to run (q039's gold was rewritten to aggregate per day for this reason). Finally it compares against the committed `eval/gold_snapshot.json`, so any gold change (from models, generator or params) is a deliberate `--update`.

### 7.2 Scoring (deterministic first)

| Score | How |
|---|---|
| `execution_accuracy` | the agent's final SQL result matches the gold result: scalars within tolerance, tables compared as sets (or ordered for `ordered_table`), column names ignored. *As built: cited results only; also via arithmetic the answer shows (see below)* |
| `answer_accuracy` | the number(s) in the final text match the gold result (extracted with regex, then compared within tolerance), which catches correct SQL that's misreported. *As built: rounding-aware, and fails if the answer leads with a wrong-definition value* |
| `metric_selection` | `metrics_used` ⊇ `expected_metrics` *(reported only; not part of pass)* |
| `refusal_correct` | for `unanswerable`: refused or caveated, with no fabricated number. *As built: a leading refusal, and every later number grounded (fix D)* |
| `caveat_present` | for estimate metrics: the answer mentions it's an estimate. *As built: required for `trap` questions only, and reported for the rest* |
| efficiency | steps, tool calls, tokens, latency, cost |

An optional LLM-as-judge scores answer clarity (1–5) as a **secondary** metric only. It never decides correctness. *(Not built yet.)*

**As built** (`evals/score.py`). The rules below supersede the summary table above where they differ.
- **Which result is scored.** Execution is judged on the results `final_answer` cites; any one match passes. A correct answer that cites no result fails execution and is labelled `answer_misreport` (E-1 q001).
  - *Tables:* every gold column must map to a distinct result column (names ignored, extra columns allowed). Rows compare as a set (`table`) or as an ordered prefix (`ordered_table`, so extra rows below a top-N are fine). A one-row gold also matches a result that lists its values in different rows, e.g. a `GROUP BY is_weekend`. It also matches when its values are each in their own cell **across the cited results** of the run (the minutes from one query, the date from another), but only if the answer text states every gold value too. *(Added 2026-11-22, owner decision, after E2b q087 r1: "26 September … about 34.8 minutes" with the minutes in r1 and the start timestamp in r2. Rescoring all nine runs flipped only that run.)*
  - *Scalars:* the gold value must appear in the result's first row.
- **Text.** Every gold value must be stated, with numbers judged by rounding at their printed precision ("~103" for 102.74).
  - Dates, date ranges, years, times and "N tables/queries" are never read as answer numbers. A range is blanked whole, so the 9 in "Mon 9 – Sun 15 Nov 2026" isn't a number.
  - Dates, hours of day and days of week match in common spellings ("26 Sep", "26 Sept", "Sept 26").
  - A ratio also matches as a percentage.
  - Seconds, minutes and hours convert into each other: in SQL results freely, but in text only when a unit word follows the number, and a bare integer never matches a scaled gold.
  - A `wrong_values` number fails the answer only if the answer **leads with it**, i.e. states it before the first unambiguous mention of the gold value it competes with (same column unit), or never states the gold. A number matching both is ignored. **Weekday labels** *(added 2026-11-22 for q097)*: a text wrong value `"<date> <Weekday>"` counts when the answer labels that date with that weekday ("Fri 14 Nov", "Friday, November 14", "14 Nov (Fri)", "2026-11-14 Friday", "| 2026-11-14 | Friday |"; a weekday merely next to a date in a comma list is not a label). The same rule applies: it fails only if it comes before the right label for that date, or the right label is missing. This was needed because a `dow` cell passes when the day's name appears anywhere in the text, and seven consecutive days use every name, so an all-shifted answer would otherwise pass. The suggested label is `answer_misreport`. Validation rejects a "wrong" label that is the date's real weekday, and the gold check rejects one for a date not in the gold. Re-scoring all 13 saved runs offline changed no result (no `rescored_to_fail`; pass rates as reported). *(Changed after the E0 review: "11,521 different videos, from 11,885 total views" is labelled context and passes; "the longest ran 62 min… estimated 35 min" leads with the wrong definition and fails.)*
- **Execution has two levels.** `execution_strict` means the gold value is literally a cell of a cited result. `execution` (used for pass) also accepts a gold value derived from a cited result by simple arithmetic: a contiguous range sum of a column, +, −, × or ÷ of two cells in the same row, or the difference or ratio of two cells in the same column. A derived value counts **only when the answer shows the arithmetic**: the text states the derived value and every cell it was computed from. **Range sums over dated rows** have their own rule. If the answer states a total and a date range (in its text or assumptions, e.g. "2026-11-09 to 2026-11-15", "10–16 Nov 2026", "Mon 9 Nov to Sun 15 Nov"), the scorer sums only the cited rows whose date falls inside that range, and the sum must match both the stated total and the gold. **Shares of a group** also count: for a result with a flag column (a boolean, or whole numbers that are only 0 and 1, as in `GROUP BY is_late_night`), the share of a numeric column's total on one side of the flag counts when the text states it. Both levels are reported.
  - *Cells that aren't values.* A whole-number cell gets no rounding slack against a ratio gold (a 0 or 1 isn't a rounded share; with half-unit slack it matched every share). A **row-label column** (whole numbers, at least 3 rows, all distinct, a run of consecutive integers: hour of day, day of week, rank) is never rescaled, so hour 17 isn't read as 17%. *(Added 2026-11-21 after the Haiku review: q060's execution check had passed in every run that cited an hourly table, through the hour 0, 16 or 17. Removing that coincidence would have failed correct answers that state the late-night share of watch time from per-hour totals, hence the group-share rule. Net effect on the committed runs: no q060 change in E1. Sonnet E0 q060 r3 passes the scorer on the watch-time reading, but the owner overrode it back to `answer_misreport`: it computes over views (1,955 / 11,885 = 16.4%), so the watch-time match is a coincidence. The owner accepted both rules on 2026-11-21.)*

  *(Changed after the E0 review, tightened after E-1, then given the range-sum rule by owner decision on 2026-11-20. "Sat 293 + Sun 186 ≈ 479 min" counts. "This week 189 min vs last week 273 min" with "This week = 2026-11-16 to 2026-11-20" counts. Two unrelated cells that happen to differ by the gold value (E-1 q044: 8.1 − 3.9 = 4.2 vs 4.18) don't count, and neither does a total matching the gold whose stated range sums to something else.)*
- **Pass rules:**
  - `unanswerable`: with no numbers stated, a refusal phrase anywhere passes. With numbers stated, the answer must **lead with a refusal** (a refusal phrase in the first sentence, which itself states no number), and every later number must be **grounded** in a run_sql result from that run: a cell, simple arithmetic as in execution scoring, or the result's row count. Ungrounded numbers fail, and so does a hedged guess like "I can't know exactly, but you probably watched 40 min". *(Changed after the E0 re-baseline review, fix D: a clear refusal followed by real, labelled context numbers is a correct refusal. Values returned by describe_table (row counts, column statistics, sample cells) also count as grounded, by owner decision after E-1.)*
  - `no_data`: says there's no data (including "no watch events for today", "no watch time is recorded for today", "no TikTok views are recorded for today" (one word may come before the noun; added after E2a, 2026-11-21), "no TikTok views for today" (added after E2b, 2026-11-22; the coverage caveat takes the same word) and "the data ends on…") and states no amount *for that day*. "0 watch events for today" fails: unknown isn't zero. Numbers in a sentence clearly about another day ("yesterday", "that day", "the day before", another date) and not about today don't count. *(Changed after the E0 review: "no data for today; yesterday you watched 103 min" passes; "you watched 0 minutes" fails.)*
  - `trap`: execution ∧ answer ∧ caveat (estimate or coverage phrases; "est." counts as "estimated", owner decision 2026-11-21).
  - `ambiguous`: execution ∧ answer **on the same reading** ∧ non-empty `assumptions`.
  - everything else: execution ∧ answer.

  `metric_selection` and caveats on non-trap estimate questions are reported, not required.
- **Strong caveat** *(added 2026-11-21, owner decision; reported only, never part of pass)*: the answer says the number is an estimate **and** gives the reason: the gaps between views, that only start times are logged, no duration, the cap, the imputed last view, or pauses. "Roughly" and "approximately" are rounding words, not caveats; "this is an estimate" or "based on per-view watch-time estimates" is a plain caveat without a reason. The two parts may be in different sentences. Reported for traps and for all estimate questions. Tests use real E0/E1 answer texts; adding it flipped no runs. *(E1 showed that the plain caveat check is easy to satisfy by labelling a number "estimated", so the stronger version shows whether the agent explains the estimate.)*
- **Checking the scorer.** An oracle mock agent (runs the gold SQL, states the result) must score 100% end to end in the tests.

### 7.3 Bad-case taxonomy
Every failed question gets one primary label: auto-suggested from the trace, then confirmed by hand in `eval/labels/<run_id>.yaml`.

**As built** (`evals/labels.py`):
- **Suggestions,** in order:
  1. run status (`tool_loop_or_step_limit`, `over_refusal`);
  2. `failed_refusal` for unanswerable and `no_data` questions;
  3. no cited result → `hallucinated_table_or_column` if a run_sql error names an unknown column or table, else `sql_error_unrecovered`;
  4. a stated wrong value → `wrong_metric_definition`;
  5. right result but wrong text → `answer_misreport`;
  6. right number but no caveat or no assumptions → `missing_caveat`;
  7. date literals differing from gold → `wrong_time_window` (or `timezone_or_day_boundary` when the question hints at it);
  8. otherwise the question's `likely_failure` hint, then `aggregation_error`.
- **Possible scoring false negatives** are flagged separately in `suspect_scoring`, so they're reviewed apart from genuine agent failures. The cases flagged:
  - a refusal or "no data" phrase *together with* numbers counted as made up;
  - a wrong-definition value alongside the gold value;
  - the right result with a failed text check, or the reverse;
  - only the caveat missing.

  *(The E0 review showed the first version missed the first two kinds.)*
- **The label file** lists one entry per failed (question, repeat), with `suggested`, `reason`, `suspect_scoring` and an empty `label` to confirm. `scrollback eval report <run>` uses confirmed labels when present.
- **Owner overrides** (added 2026-11-21). When the scorer's verdict is right by its rules but wrong on reading the answer, the owner can reverse it for one (question, repeat): an `overrides` entry in the label file with `passed`, a `label` when it fails, and a `reason`. `eval rescore` applies it, keeps the scorer's verdict as `passed_scored` in the record, and counts the override in every summary. Overrides are for coincidences the rules can't see; a pattern that recurs gets a scoring rule instead. The only one so far: Sonnet E0 q060 r3, which states "about 17%" computed over views (1,955 / 11,885 = 16.4%) and so matches the watch-time share only by coincidence.
- **Review practice** (Phase 4):
  - Every failure is read by hand. Genuine agent failures are reported separately from suspected scoring false negatives and from question-design problems.
  - Only the owner confirms labels. Suggested labels are heuristics, and for an ablation they can be wrong across the board: E-1's date-literal rule suggested `wrong_time_window` for 56 runs, which were relabelled from the answers.
  - A failure whose scoring is under open discussion keeps `label: null` and a `note` until it is decided.
  - Scoring and question fixes are approved by the owner, tested on real answer texts, and applied to every affected run with `eval rescore`. Runs that flip are reported.

`wrong_metric_definition` · `wrong_time_window` · `timezone_or_day_boundary` · `hallucinated_table_or_column` · `join_or_grain_error` · `aggregation_error` · `sql_error_unrecovered` · `answer_misreport` · `missing_caveat` · `failed_refusal` · `over_refusal` · `tool_loop_or_step_limit`

### 7.4 Runs and reports
`scrollback eval run --config experiments/e03_rag.yaml --repeat 3`
- Writes `eval/runs/<timestamp>_<name>/results.jsonl` and `summary.json`.
- **As built:** `scrollback eval run --config eval/experiments/<exp>.yaml [--split dev|test|all] [--repeat 3] [--workers 4] [--only q001,…] [--yes]`.
  - **Cost gate:** without `--yes` it only prints a cost estimate, from past traces of the same model × 1.3 for the harder set, plus a p90-based upper figure. Nothing calls the API until `--yes`. *(Added.)*
  - **Experiments** override only the `llm` and `agent` config sections, so each one is a config diff. The set's pinned params always apply.
  - **Run directory:** `config.json` (resolved non-secret config, experiment, question ids, git commit), `results.jsonl`, `traces.jsonl` (eval runs write traces here, not to `agent_traces`), `summary.json` and `report.md`.
  - **Labels:** a label file is written to `eval/labels/`.
  - `--app-config` replaces the global `--config` for this command, since `--config` names the experiment.
- **`scrollback eval rescore <run>`** re-scores a finished run from its saved answers and cited results (up to 200 rows each) with the current questions and scorer. It makes no API calls. The first re-score keeps `results.original.jsonl`. Changes are always reported against the original scoring (`rescored_to_pass` / `rescored_to_fail` in the label file), and confirmed labels and notes carry over.
- **Report:** accuracy is mean (min–max) over repeats. Per-category accuracy is shown for **dev only**; the held-out **test** split is reported overall only, since it has too few questions per category. The report also covers sub-scores, efficiency, cost, the bad-case distribution, every failure with its label and reason, per-question pass counts and flaky questions.
- `scrollback eval report <run_a> <run_b> [--split dev|test|all]` produces markdown with overall and per-category accuracy, the questions that regressed or improved, the sub-scores (including the strong caveat) and efficiency (steps, tool calls, tool errors, tokens, latency, cost per run and total). The comparison is summarised from both runs' records, so `--split dev` compares a dev-only run with the dev half of a full run on equal terms. *(Changed 2026-11-21: it used to compare the saved summaries, so a dev-only run's cost was set against a full run's.)*
- Each config is run with `--repeat 3`, and the mean ± spread is reported, because LLM output varies even at temperature 0.
- **Retrieval recall (Phase 5, reported only).** Each question carries `expected_knowledge`: the chunk ids a good search should find. They are written from the definitions by rule, not from retrieval output:
  - each expected metric's chunk;
  - the sessions section that separates session length from the span, for session-length questions;
  - the midnight-crossing section where sessions are dated by their start;
  - the estimate caveat or method sections for estimate traps;
  - coverage for partial or missing days;
  - `agg_daily` for per-calendar-day averages;
  - the table where one column is the crux;
  - the cannot-answer sections for unanswerable questions.

  Only expected chunks within the run's `kb_sources` count, so unanswerable questions have no recall before E6. Two measures:
  - **offline** `scrollback eval retrieval [--k] [--split dev] [--experiment]` searches with each question's own text: recall@k per category on dev, overall only on test. No API calls.
  - **per run**, the union of the chunks the agent's own `search_knowledge` calls returned, scored against `expected_knowledge` (`kb_recall`). Reported with the share of runs that searched at all (`kb_searched`) and searches per run.

### 7.5 Planned experiment ladder
Each step is one config. Results go into the README table.

| ID | Change | Hypothesis |
|---|---|---|
| E-1 | **warehouse ablation**: the E0 agent and prompt, but only the staging views (`stg_*`) may be queried (`agent.tables: staging`); no sessions, watch-time estimates, flags or zero-filled spine | quantifies what the data modelling alone contributes (E0 − E-1) |
| E0 | schema only, `run_sql` only | baseline |
| E1 | + table and column descriptions (`describe_include_descriptions: true`) | fewer hallucinated columns |
| E2a | + `search_knowledge` tool over the metrics, glossary and table descriptions (hybrid retrieval); prompt v1 with only its tool list extended by the new tool, no new rule | does the agent look definitions up unprompted, and do fewer definitions go wrong |
| E2b | + the rule "look up the definition of any metric before computing it" (`system_v2.md`) | fewer wrong metric definitions than E2a |
| ~~E3~~ | ~~+ few-shot examples via RAG~~ *(skipped, see below)* | — |
| ~~E4~~ | ~~+ richer structured SQL errors~~ *(cut, see below)* | — |
| ~~E5~~ | ~~+ `query_metric` semantic-layer tool~~ *(moved to the stretch backlog, see below)* | — |
| E6a | + caveat, refusal and "unknown isn't zero" rules, as `system_v3.md`; built on E2b, same knowledge sources | fewer failed refusals and missing caveats, check over-refusal |
| E6b | + the "cannot answer" doc in the knowledge index (`kb_sources` adds `cannot_answer`) | the rules work better with the doc to retrieve; check that its chunks don't crowd out others |
| ~~E7~~ | ~~model swap at the end~~ *(moved to the base of the ladder, see below)* | — |

*(E-1 added, 2026-11-20, after E0 scored ~95%: much of E0's strength appears to come from the marts encoding the definitions as columns (`est_watch_sec`, `is_binge`, `is_late_night`, `is_active`). E-1 measures that directly. The SQL guard, `describe_table` and the prompt's table list all follow `agent.tables`. In the staging scope no table counts as pre-aggregated, so every query must aggregate.)*

*(E4 changed, 2026-11-20: basic self-correction, where the error goes back to the model with up to `sql_max_retries` = 2 retries, is already part of the E0 baseline, so E4 now measures richer error content instead. E0's errors are the guard's rule code plus DuckDB's or the guard's message.)*

*(E4 cut, 2026-11-20, owner decision at the start of Phase 5: E0 has no `sql_error_unrecovered` and no `hallucinated_table_or_column` failures in 288 runs, so richer error messages have nothing to fix and could only move tokens and steps. The ladder spends its runs on the failures that exist. E4 can come back if a later config produces SQL-error failures.)*

*(E3 skipped, 2026-11-22, owner decision: no headroom. E2b passes 191 of 192 dev runs (99.5%). The only failure left is q072's "0 minutes" for a day with no data, which is a refusal failure, not a SQL one. The relative-date failures examples were meant to fix (q019, q080) and q052's rounding pass 3/3 in E2b, so examples could only move tokens and steps, and writing them carries a leak risk that the overlap check would only partly control. E3 can come back if the test split or a real export shows SQL-pattern failures.)*

*(E6 split into E6a and E6b, 2026-11-22, owner decision: one change per step, so the rules' effect is measured apart from the doc's. Offline recall@6 on dev with the `cannot_answer` source added shows the reason. Unanswerable questions come into scope at 16.7%, but ranking falls from 100% to 70% and trap from 86.1% to 76.4%, as its chunks crowd others out.)*

*(E5 moved to the stretch backlog, 2026-11-22, owner decision: for the same reason, a `query_metric` tool has no dev failures left to fix. It stays in the design (§6.2) as a stretch item.)*

**Model swap at the base (owner decision 2026-11-21).** E1 on Sonnet scored 99.5% on dev (one failing run in 192; `eval/reports/e01_descriptions.md`), so later steps can't show accuracy gains on Sonnet. The model swap therefore moves from the end of the ladder to its base:
1. **E0-haiku** and **E1-haiku**: the E0 and E1 configs with `model: claude-haiku-5-5`, dev split, reported against Sonnet E0 and E1.
2. If E1-haiku has real headroom (dev below about 93%), E2a → E6 run **on Haiku**, under the ladder rules below. Accuracy is the primary measure, with tokens, steps and recall@k alongside.
3. **Sonnet's final config is E1.** Sonnet is re-run with a later step only if the Haiku results suggest that step would help it.
4. **Final:** the best Haiku config and Sonnet E1 each run once on the test split.

Opus is no longer on the ladder.

**Result (2026-11-21, `eval/reports/e00_e01_haiku.md`):** E0-haiku 90.1% and E1-haiku 97.9% (95.3–100) on dev, against Sonnet E0 91.7% and E1 99.5%; $0.0008 per question on Haiku against $0.0105 on Sonnet. E1-haiku is above the ~93% headroom threshold, but **the owner chose to run E2a → E6 on Haiku anyway**, so the RAG steps are measured. They are expected to show mostly in tokens, steps, recall@k and the strong-caveat sub-score: 4 failing runs are left (q019 ×2, q052, q072).

**Done when, for Haiku** (owner decisions 2026-11-21):
- **Met at E1-haiku:** the mean dev pass rate is within about 2 points of Sonnet E1 (97.9% vs 99.5%), with cost per question reported next to Sonnet's ($0.0008 vs $0.0105).
- **From E2a on, each step is judged on:**
  - no dev accuracy regression beyond repeat variance against the previous config (its best repeat not below the previous config's worst);
  - recall@k (offline, and per run from the agent's own searches);
  - tokens and cost per question;
  - which of E1-haiku's 4 remaining failures it fixes (q019 r2/r3, q052 r3, q072 r3), and whether it breaks anything that passed.
- *Replaced:* "the best Haiku config beats E1-haiku beyond repeat variance (its worst repeat above E1-haiku's best)". It couldn't be met, since E1-haiku has a 100% repeat.

**Ladder rules (Phase 5, owner decisions 2026-11-20, 2026-11-21):**
- **One experiment, one change.** Each config differs from the previous one by a single change. E2 is split into E2a (tool available) and E2b (plus the lookup rule), so the rule's effect is measured apart from the tool's.
- **Cumulative.** Each step builds on the previous config. A step is dropped from later configs only if its dev pass rate is worse than the previous config's beyond repeat variance (its best repeat below the previous config's worst).
- **Dev only while iterating.** The test split runs once, on the final config.
- **Prompt versions:** `system_v1.md` is never edited; E2b adds `system_v2.md`, E6a adds `system_v3.md`.
- **Done when** (for Sonnet; replaces "beats E0 on execution accuracy", since E0's execution score is already 99.2% and its failures are in the answer text):
  - the best config's mean dev pass rate is above E0's best dev repeat, and its worst dev repeat is at least E0's dev mean;
  - the session-length cluster (dev: q023, q042, q084, q087) passes at least 10 of its 12 dev runs;
  - tokens per question are reported next to accuracy;
  - the final test pass rate is at least E0's 96.9% (E0's only test failures are q062 × 3, so test can gain at most 3.1 points).

---

## 8. Interfaces

### CLI (`scrollback`)
`ingest`, `synth`, `build` (dbt), `docs` (dbt docs site), `inspect gaps`, `ask "<question>" [--show-sql] [--chart] [--today] [--experiment f | --no-experiment]`, `eval run|report|rescore|retrieval|check-gold|import-feedback`, `kb index|search`, `alerts check`, `digest send`, `serve api`, `serve bot`.

`docs` *(added, Phase 7)* runs `dbt docs generate --static` with the same `--vars` as `build` and writes one self-contained HTML file (lineage graph, model and column descriptions) to the git-ignored `dbt/target/docs/`. It is built locally, not hosted *(owner decision 2026-11-22; GitHub Pages can follow once the repo is public)*. Build it from the synthetic warehouse (`--config config.sample.yaml`) if it will be shared.

**Quickstart config** *(added, Phase 7)*. `config.sample.yaml` is `config.yaml` with the eval set's pinned metric parameters and its own warehouse file (`data/warehouse/sample.duckdb`). The README quickstart passes it with `--config`, so synthetic answers follow the eval setup (180 s cap) and never touch the owner's real warehouse. `tests/test_config.py` keeps it equal to `eval/questions.yaml` `params` and to `config.yaml`'s other sections.

`inspect gaps` *(added)* prints the distribution of gaps between consecutive views, for tuning `session_gap_min` and `view_cap_sec` on a real export (§14). It shows the count, percentiles for all gaps and for within-session gaps, and a histogram whose bin edges include the current parameter values. It also prints two what-if tables: how many sessions each candidate `session_gap_min` would give, and what share of within-session gaps each candidate `view_cap_sec` would cap. It prints aggregates only (no timestamps, IDs or rows), so it's safe on real data.

### HTTP API (FastAPI) *(stretch)*
- `POST /ask {question} → {answer, sql[], metrics[], assumptions[], chart_url?, trace_id}`
- `GET /metrics` (the glossary) · `GET /health` · `GET /charts/{id}.png`
- Local only by default, with a bearer token from env.

### Telegram bot
- Free-text message → agent → reply with the answer, chart as a photo, and an inline "Show SQL" button.
- Commands: `/today`, `/week`, `/metrics`, `/feedback <good|bad> [note]`, which attaches feedback to the trace and feeds bad cases. `/wrapped` is *stretch* (it needs the weekly digest, §9).
- *(stretch)* Scheduled jobs: a daily anomaly check and the Monday digest.

**As built (Phase 6)** in `src/scrollback/bot/` and `src/scrollback/feedback.py`; started with `scrollback serve bot`:
- **Config.** The bot runs the interactive config (§6.7): `config.yaml` + the `llm`/`agent` overrides of `interactive.experiment` (Haiku E6b by default) on the calibrated real params. `bot:` holds `poll_timeout_sec`, `chart_keywords` and `drop_pending_on_start`. Secrets come from `.env`.
- **Transport.** A small Bot API client over `httpx` (`bot/telegram_api.py`): long-polling `getUpdates`, `sendMessage`, `sendPhoto`, `sendChatAction`, `answerCallbackQuery`. *(Chosen over python-telegram-bot: five calls, synchronous like the agent, trivial to fake in tests with `httpx.MockTransport`, and full control of logging.)* Updates are handled one at a time, which is fine for one user.
- **Allowlist.** Only chats in `TELEGRAM_ALLOWED_CHAT_IDS` get any reaction. Every other update (message or button press) is dropped silently: no reply and no callback answer, and the log counts it without the chat ID. The bot refuses to start with an empty allowlist, a missing token, or `agent.result_mode` other than `aggregate_only` (§4.6).
- **Start-up (`scrollback serve bot [--experiment f | --no-experiment]`).** Checks the token, the allowlist, `aggregate_only` and that the warehouse has data, and names every problem before exiting. It builds or loads the knowledge index before polling, so the first question isn't slow, then prints the config in use (experiment, model, prompt version, effort, number of allowed chats, warehouse file name). An experiment's `agent` overrides win over env variables such as `AGENT_RESULT_MODE`, and E6b pins `aggregate_only`, so an env setting can't switch the default bot to row-level results.
- **One bot at a time** *(added 2026-11-22, after several bots were left running during the phone check)*. Telegram lets only one client long-poll a token. With two pollers, each `getUpdates` cancels the other's with HTTP 409 Conflict, and messages and button taps go to whichever happens to be polling. So `serve bot` first takes `traces/bot/serve_bot.lock`, created atomically (`O_EXCL`), holding the PID, start time, host and git commit. A second `serve bot` refuses to start and names the running bot's PID, start time and commit. A lock whose process is gone, or whose PID now belongs to another program (checked with `ps`), is stale and is taken over. `SIGTERM` is turned into a clean stop so a plain `kill` releases the lock. The start-up line prints the PID and commit, so "which code is this bot running?" has an answer. A bot on another machine can't be seen by the lock, so a 409 from `getUpdates` logs one clear error per conflict streak ("another process is polling this bot token … stop the other one"), repeated every 20 consecutive conflicts, and the bot keeps retrying with backoff.
- **Messages sent while the bot was off** are skipped on start (`drop_pending_on_start`), so none trigger paid runs unseen.
- **Command menu.** On start the bot registers `/today`, `/week`, `/metrics`, `/feedback` and `/help` with `setMyCommands`, so Telegram offers them when you type "/". A failure there is logged and the bot keeps running.
- **Free text.** The reply is the answer text, its assumptions (which carry the resolved dates), the metrics used, and a footer with the trace ID, serving model, cost and latency. Statuses other than `answered` reply with the CLI's status message and the trace ID, never a number. A chart is requested only when the message contains a `chart_keywords` word (chart, plot, graph, trend, visualise) or for `/week`, because E6b was evaluated without chart requests. A chart the agent makes anyway is still sent. The chart goes as a photo replying to the answer.
- **Show SQL.** The inline button carries `sql:<trace_id>` (under Telegram's 64-byte limit). Pressing it sends the answer's `sql_used` from the trace JSONL: the SQL that actually ran, after the guard's rewrite (§6.1).
- **`/today` and `/week`** are fixed agent questions about the most recent data, because an export always ends before the day it is used (owner decision 2026-11-22): `/today` asks "How much did I watch on the most recent day in my data?" and `/week` asks for "my watch time for the last 7 days of data, by day, with a chart". Both ask the agent to state the dates it used, and the assumptions shown with every answer carry the dates too. *(Changed after the phone check, owner decision 2026-11-22: `/week` labelled all seven days one weekday off, "Fri 14 Nov" for a Saturday. Its canned text now ends "Take weekday names from dim_date.dow_name; don't work them out. State which dates you used." This is bot-level only: the system prompt and the eval config are unchanged. The general fix is the `resolve_dates` stretch item.)* They go through the agent rather than fixed SQL so they get a trace, Show SQL and feedback like any question.
- **`/metrics`** lists `knowledge/metrics.yml` (watch-time method, then each metric's label, name and description) with `{param}` filled from the bot's settings. No LLM call.
- **👍 / 👎 buttons** sit under every answer, including non-`answered` statuses, next to Show SQL. Their callback is `fb:good:<trace_id>` or `fb:bad:<trace_id>`, so they rate exactly that answer whatever was sent since (owner request 2026-11-22, after the misattributed `/feedback`). 👍 records `good` and confirms in a toast that quotes the question. 👎 records `bad`, then sends a "What was wrong?" message with Telegram's force-reply. A text reply to that message is stored as a second `bad` line carrying the note (the latest line per trace wins, §8 import). A command sent in reply still runs as a command. Note prompts are kept in `traces/bot/messages.jsonl` (`kind: note_prompt`), so they survive a restart. Callbacks from chats not on the allowlist are ignored like messages.
- **`/feedback good|bad [note]`** rates the answer it replies to, or else the chat's last answer. The confirmation quotes the rated question, so a rating that landed on the wrong answer is visible at once *(added after the phone check: a reply sent from Telegram's command menu arrived without its reply link and rated the last answer instead)*. A reply can target the user's question, the answer, its chart or its Show SQL message, because all four are mapped to the trace. *(Widened after the phone check on 2026-11-22: a reply to the user's own question was rejected when only the bot's answer and chart were mapped.)* It is written to `traces/feedback/feedback.jsonl` (the source of truth) and, best effort, to an `agent_feedback` table beside `agent_traces` (they join on `trace_id`). The bot's message → trace map is `traces/bot/messages.jsonl`, so replies still resolve after a restart. *(Changed from the plan's `bot_messages` table: an append-only file can't hit the warehouse lock.)*
- **`scrollback eval import-feedback`** (`evals/feedback_import.py`) turns bad feedback into candidate eval questions. For every trace whose *latest* rating is `bad`, it appends one entry to `eval/candidates/feedback.yaml`: the feedback, the question and its `today`, status, model and prompt hash, the answer with its assumptions, metrics and SQL, the tool error codes, any eval question with the same text, and a `proposed` `questions.yaml` stub whose category, anchor and gold are `TODO`. It **never writes `questions.yaml`**. The owner reviews each entry, copies accepted ones by hand, picks a synthetic `today` anchor and writes the gold from the definitions (§7.1 writing rules), then runs `check-gold --update`. The file is **git-ignored** (owner decision 2026-11-22), because it holds real questions and answers. Re-runs add only new traces and keep the owner's edits. A bad rating whose trace isn't found yet is retried on the next run. An entry whose rating has since turned `good` is flagged, not deleted. The command prints counts only.
- **Logging and privacy.** Logs carry trace IDs, statuses, costs and error *types* only: no question text, answers, query results, chat IDs or the token. The token is part of every Bot API URL, so the client keeps the `httpx`/`httpcore` loggers at WARNING (at INFO they log each URL), and also `httpx2`, the Anthropic SDK's own httpx copy, which logged API URLs (no key) in the first real run. `serve bot` also mutes matplotlib's INFO messages, redacts it from every error, and `serve bot` adds a redacting log filter. A test plants a fake token, forces API errors and checks the logs and traces.

### Demo script (3 minutes)
The rehearsal script with exact commands and messages is [DEMO.md](DEMO.md). *(Owner decision 2026-11-22, Phase 7: steps 1 and 2 and the README GIF use the synthetic sample; steps 3 and 4 use the live bot on the real export, aggregates only.)*
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
├── config.sample.yaml       # quickstart: synthetic warehouse + eval params
├── .env.example
├── .github/workflows/ci.yml
├── docs/
│   ├── DESIGN.md
│   ├── ROADMAP.md
│   └── DEMO.md              # three-minute demo script (§8)
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
│   ├── warehouse/{db.py, dbt_runner.py, inspect.py}
│   ├── kb/{chunks.py, index.py, retrieve.py}
│   ├── semantic/{metrics.py, compile.py}  # metrics.yml schema + loader; → SQL compiler (Phase 5)
│   ├── agent/{loop.py, tools.py, sql_guard.py, llm.py, prompts/}
│   ├── charts.py
│   ├── tracing.py
│   ├── evals/{questions.py, warehouses.py, gold.py, score.py, run.py, labels.py, report.py, retrieval.py, feedback_import.py}
│   ├── monitor/{anomaly.py, digest.py}
│   ├── api/app.py
│   ├── experiments.py      # experiment files; interactive config for ask and the bot (§6.7)
│   ├── feedback.py         # /feedback store, trace lookup, bot message map (§8)
│   └── bot/{telegram_api.py, telegram_bot.py}
├── eval/                    # eval data; the code lives in src/scrollback/evals/ (importable, typed)
│   ├── questions.yaml
│   ├── gold_snapshot.json
│   ├── experiments/*.yaml
│   ├── runs/                # git-ignored except selected summaries
│   ├── labels/
│   ├── candidates/          # git-ignored: candidate questions from bot feedback (§8)
│   └── reports/             # committed baseline/experiment reports
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
- ~~Is the export timestamp actually UTC?~~ Yes (§4.1 item 6, confirmed 2026-11-22).
- Which export variants does the parser need to support? Collect one real export early.
- ~~Should `is_rewatch` count a video as a rewatch on any repeat, or only after a gap?~~ Any repeat (§4.4).
- What should the default binge threshold and session gap be? Tune them by looking at the distribution of your real gaps.
- ~~**Date spine vs. history windows**~~ *(resolved 2026-11-22: the spine now covers the watch-history window only, §4.4)*. `dim_date` and `agg_daily` start at the earliest record of *any* dataset. In the real export that is the first like, more than a year before the watch history starts, so roughly three quarters of the spine lies outside the watch-history window. Those days look like zero-view days when they are unknown (§4.1 item 13), which skews daily averages, active days and streaks. Synthetic data didn't show it because every dataset shares one window. A likely fix is to bound the spine by the watch history's first and last dates (or flag days outside it); check whether it moves any eval gold before changing it.
- **Gaps between exports look like zero-view days** *(noted 2026-11-23)*. An export covers roughly the last six months of watch history. If two imports are further apart than that, the days between them have no records, but `dim_date` spans the first to the last view without gaps and `agg_daily` zero-fills, so they read as days without viewing rather than unknown ones. The README asks for an export at least every two to three months. A fix would mark days outside every ingested export's window as unknown (for example from `ingest_log` coverage), and must not move eval gold.
