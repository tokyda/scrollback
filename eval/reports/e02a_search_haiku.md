# E2a on Haiku: `search_knowledge` available (dev)

Run `20261121T070131Z_e02a_search_haiku`: 64 dev questions × 3 repeats on `claude-haiku-5-5`,
effort medium, at commit `a819d3d`. Compared with E1-haiku (`20261120T171107Z_e01_haiku`).

**The one change:** E1-haiku plus the `search_knowledge` tool. The prompt is `system_v1_search.md`,
which is v1 plus one line naming the tool; it adds no rule about when to use it. The tool returns
the top 6 chunks from metric definitions (`metrics.yml`), glossary sections (`knowledge/docs/`,
without `cannot_answer.md`) and mart table descriptions, 39 chunks in all. Ranking combines
bge-small embeddings with BM25 keyword and synonym matching (RRF). Rendered prompt SHA-256:
`18413edf…6f45e`.

The step is judged on the per-step criteria the owner set on 2026-11-21 (DESIGN §7.5):
- no dev regression beyond repeat variance;
- recall@k;
- tokens and cost per question;
- which of E1-haiku's 4 failures it fixes.

## Headline (dev)

| | E1-haiku | E2a | Change |
|---|---|---|---|
| **Dev pass rate** | 97.9% (95.3–100) | **97.9%** (96.9–98.4) | 0.0 |
| Failing runs | 4 | 4 | |
| Session-length cluster (q023, q042, q084, q087) | 12/12 | 12/12 | |
| Tokens per run | 10,917 | 14,401 | +32% |
| Cost per run | $0.0008 | $0.0011 | +38% |
| Cost for 192 runs | $0.16 | $0.21 (estimate $0.21) | |
| Steps / tool calls | 3.11 / 4.28 | 3.14 / 4.80 | +0.52 calls (the search) |
| Latency | 5.2 s | 5.1 s | |

- **No regression beyond repeat variance:** E2a's best repeat (98.4%) is above E1-haiku's worst
  (95.3%), so E2a stays in the ladder.
- **No accuracy gain either:** the same mean, with a narrower range (62, 63 and 63 of 64).
- **The cost is about a third more tokens.** Each search returns 6 full chunks, and the agent
  searches in 98% of runs (1.13 searches per run).

| Dev category | E1-haiku | E2a |
|---|---|---|
| relative_date | 92.6 | 96.3 |
| multi_step | 93.3 | 100 |
| trap | 94.4 | 83.3 |
| the other 8 categories | 100 | 100 |

## E1-haiku's 4 failures, and what changed

| E1-haiku failure | E2a | Attributable to search? |
|---|---|---|
| q019 r2, r3 (`wrong_time_window`, "last weekend") | 3/3 pass | **Unlikely.** The knowledge base says nothing about "last weekend", and the agent searched for "watch minutes metric". E0-haiku also passed q019 3/3, so this looks like repeat variance. |
| q052 r3 (`aggregation_error`, rounded in SQL before subtracting) | 3/3 pass | **Unlikely.** Rounding isn't in the knowledge base; q052 was 1/3 on E0-haiku and 2/3 on E1-haiku. |
| q072 r3 (`failed_refusal`, "0 minutes" for today) | 0/3 pass | **No.** The agent never retrieved the coverage section (searches: "watch time duration today"). |

**Regressed:** q072 2/3 → 0/3 and q080 3/3 → 2/3. **Improved:** q019 1/3 → 3/3 and q052 2/3 → 3/3.

## Failures (proposed labels; owner to confirm)

| Q | Rep | Proposed | What happened |
|---|---|---|---|
| q072 | 1 | `failed_refusal` | "I can't find any TikTok views for today … so the answer is 0 minutes." Unknown isn't zero (fix C). |
| q072 | 3 | `failed_refusal` | Leads with "No data for today", then "today's estimated watch time is 0 minutes so far" (fix C). |
| q072 | 2 | **suspected scoring false negative** | "No TikTok views are recorded for today …, so there is no watch time to report." It states no amount, but the no-data phrase list misses "No TikTok views are recorded". |
| q080 | 1 | `wrong_time_window` | Read "since the start of last month" as October only, and said so in its assumptions (gold: 1 Oct – today). |

**Suspected scoring false negative (not fixed; needs your OK).** The fix would widen the no-data
phrase from "no (watch time | minutes | activity | views | data) (is | was | …) recorded" to
allow one word before the noun ("no TikTok views") and "are" as the verb. It would come with a
test from this answer text, a rescore of all runs, and a flip report. If accepted, E2a would be
98.4% (189/192) and q072 1/3.

The two q072 "0 minutes" runs are the failure E6 (refusal rules, with the `cannot_answer` doc)
is meant to address. E2a's knowledge base doesn't include that doc.

## Retrieval

**Offline recall@6** (`eval retrieval --split dev`; the query is the question text):

| Slice | recall@6 | all found |
|---|---|---|
| **all** | 79.3% | 40/58 |
| metric_definition, ranking, relative_date, simple_agg, time_window | 100% | 29/29 |
| multi_step | 70.0% | 2/5 |
| trap | 63.9% | 2/6 |
| ambiguous | 58.3% | 3/6 |
| session_derived | 52.1% | 2/8 |
| comparison | 50.0% | 2/4 |

Six dev questions expect only `cannot_answer` chunks, so they are outside E2a's sources.

**In the runs** (the agent's own queries):
- recall of `expected_knowledge`: 87.6%;
- runs that searched: 97.9%;
- metric selection: 0% → 59.1%. The agent now puts metric names from the search results in
  `metrics_used`; before, it wrote column expressions there.

The agent's queries are short and generic ("watch minutes metric", "searches count definition",
"night time usage definition"). They do better than the raw question text because they name the
concept.

**What the offline misses show:**
1. **One hub chunk.** `doc:timezone_and_day_boundary#sessions-that-cross-midnight` is in the top 6
   for most dev questions, including ones unrelated to sessions or midnight. It is also in 69 of
   the 192 runs' results. It mentions many common identifiers and words (local date, hour, day,
   session, watch), so it matches both rankings weakly for almost any query. It takes a slot
   that the right table or caveat section would otherwise get.
2. **Caveat and coverage sections rank low.** `watch_time_estimation#caveats-to-state…` is missed
   on q071, q074 and q075, and `timezone_and_day_boundary#coverage` on q072 and q074. A plain
   question ("how long did I watch today") doesn't contain the words those sections are about.
3. **Table chunks lose to metric chunks.** `table:agg_daily` and `table:fct_sessions` are missed
   when a metric chunk covers the same words (q030, q049, q055, q085).

I haven't changed retrieval. Tuning it against these dev misses would fit the retrieval eval
rather than improve retrieval. If you want the hub chunk handled, a rule that doesn't look at
the questions would be to weight only a chunk's own heading and names as keywords, not every
backticked identifier in its body. It would be its own change, measured with `eval retrieval`
before any agent run.

## Disclosures

- **Glossary edit before E2** (`2fa833a`): `knowledge/docs/sessions.md` now says session length is
  `est_watch_sec`, not the first-to-last-view span. It is a definition, not a question-specific
  hint, but it is the text that the session-length questions depend on.
- **Ad-hoc searches while building the index:** about 9 test searches, one of them q062's exact
  text (a test-split question). `expected_knowledge` was written afterwards, by rules from the
  definitions (DESIGN §7.4).
- **The `avg_session_minutes` synonym "typical session length"** matches q062's wording. It was
  committed in `metrics.yml` (`d870c07`) before the eval questions existed (`06cac28`), so it
  isn't a leak.

## Implications

- **On Haiku, search alone adds cost without measurable accuracy.** E1's table and column
  descriptions already carry the definitions that fixed the session-length cluster, and the
  remaining failures aren't about definitions.
- **E2b** (`system_v2.md`, the "look up definitions before computing" rule) is the next step. The
  agent already searches in 98% of runs, so the rule is unlikely to change how often it searches.
  It may change whether the agent uses what comes back, for example the caveat section.
- **E6** is the step aimed at q072. E3 (examples) is the one that could move q019 and q080
  (relative-date windows), provided the examples pass the overlap check.
