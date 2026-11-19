# Sessions

**Tables:** `fct_sessions` (one row per session), `fct_views.session_id`.
**Metrics:** `sessions`, `avg_session_minutes`, `binge_sessions`.

## Definition

A **session** is a run of views in time order where each gap between consecutive views is at
most {session_gap_min} minutes. The first gap longer than that starts a new session. Gaps are
measured in UTC, so daylight-saving changes do not create or hide gaps. Two views in the same
second are ordered by `view_id`.

- `session_id` is the `view_id` of the session's first view.
- `start_ts_local` / `end_ts_local` are the local start times of the first and last view.
  `end_ts_local` is **observed**, not an estimated end: the last view's own watch time is not
  added.
- `n_views` is the number of views; `est_watch_sec` is the sum of their estimated seconds
  (see watch time estimation).
- `local_date` is the local date the session **started** (see timezone and day boundary).
- `is_binge` is true when `est_watch_sec` is at least {binge_threshold_min} minutes.

## Examples

With a 30-minute session gap:

- Views at 10:00:00, 10:01:10 and 10:31:10 (gaps of 70 s and exactly 30 min) are **one**
  session.
- A view at 10:31:25 followed by one at 11:01:26 (30 min 1 s later) ends the session; the
  11:01:26 view starts a new one. If nothing follows within 30 minutes, it is a single-view
  session.

## Things to keep in mind

- Session counts depend directly on the gap threshold: a shorter threshold gives more,
  shorter sessions. `scrollback inspect gaps` shows how many sessions each threshold gives.
- A session is a viewing streak inferred from gaps, not an app open/close event; the export
  has no app-open data.
- Session length is an estimate because it is a sum of estimated view times.
