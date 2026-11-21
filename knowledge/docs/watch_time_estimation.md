# Watch time estimation

**Metric:** `est_watch_minutes` (and everything built on it: `avg_session_minutes`,
`binge_sessions`, `agg_daily.est_watch_min`, `fct_views.est_watch_sec`).

## The problem

The TikTok export records **when each video started**, never how long it was watched. There
is no duration field. Any "watch time" number is therefore an **estimate**, and every answer
that uses one must say so.

## The method (gap-based, version 1)

Views are put in time order. The time between one view and the next is the *gap*.

1. **Sessions.** A gap longer than {session_gap_min} minutes ends a session; the next view
   starts a new one. A gap of exactly {session_gap_min} minutes stays in the same session.
2. **Every view except the last in its session** gets `min(gap to the next view,
   {view_cap_sec} seconds)`. Scrolling to the next video is taken as the moment you stopped
   watching this one; the cap stops a long pause from counting as a long watch.
3. **The last view in a session** has no next view, so its time is imputed: the **median** of
   the other views' estimates in that session. With an even number of other views the median
   is the average of the middle two. A session with a single view gets
   {default_last_view_sec} seconds.
4. Session, daily and period totals are sums of the per-view estimates.

## Worked example

Parameters: session gap 30 min, cap 180 s, single-view default 20 s. Five views:

| View | Time | Gap to next | Estimate |
|---|---|---|---|
| 1 | 10:00:00 | 40 s | min(40, 180) = 40 s |
| 2 | 10:00:40 | 30 s | min(30, 180) = 30 s |
| 3 | 10:01:10 | 30 min exactly | min(1800, 180) = **180 s** (capped) |
| 4 | 10:31:10 | 15 s | min(15, 180) = 15 s |
| 5 | 10:31:25 | 30 min 1 s | last in session: median(40, 30, 180, 15) = (30 + 40) / 2 = **35 s** |

The 30-minute pause after view 3 does not split the session (exactly the threshold), but it
only counts 180 s of watching. The gap after view 5 is one second over the threshold, so the
next view starts a new session. Session total: 40 + 30 + 180 + 15 + 35 = **300 s = 5 min**.

## Caveats to state with any watch-time number

- It is an **estimate**: the export has no durations.
- Pauses of up to {view_cap_sec} seconds between views count as watching.
- A view counts for at most {view_cap_sec} seconds, so long videos are undercounted.
- Background or audio-only play without a new view event is invisible.
- The last view of each session is imputed, not measured.

## Parameters

All live in `config.yaml` (`metrics:`) and are passed to dbt; they can be tuned with
`scrollback inspect gaps`. Current values: session gap {session_gap_min} min, cap
{view_cap_sec} s, single-view default {default_last_view_sec} s.

The cap was calibrated against one week of the owner's phone Screen Time for TikTok (the app
is only on that phone). Screen Time counts all time in the app, including comments, DMs,
search, LIVE and browsing, so the watch-time estimate should come out somewhat **below**
Screen Time, never be tuned to match it. A larger cap put the estimate above Screen Time, so
it was lowered to a round value that puts the estimate below it. Treat Screen Time as an upper
bound on watch time, not as the true value.
