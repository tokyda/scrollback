# Timezone and day boundary

## Timezones

- Export timestamps are assumed to be in `{source_tz}` (unverified against a real export).
- Every date and hour in the marts (`local_date`, `local_hour`, `dow`, `is_weekend`,
  `is_late_night`) is in the local timezone **{local_tz}**.
- Gaps and session boundaries are computed in UTC, so daylight-saving changes cannot distort
  them.
- Relative dates in questions ("yesterday", "last week") resolve in {local_tz}. Weeks run
  Monday to Sunday (ISO); `dim_date.iso_week` looks like `2026-W47` and `week_start` is its
  Monday.

## Day boundary

A day starts at **local midnight**. A view at 00:30 belongs to the new date, not the previous
evening. "Late night" means local hours [{late_night_start_hour}, {late_night_end_hour}), so
after-midnight viewing shows up both as late-night views and on the new date.

## Sessions that cross midnight

A session can start before midnight and end after it. Two different rules apply, on purpose:

- **Minutes and views are credited to each view's own date.** Daily watch time
  (`agg_daily.est_watch_min`, `est_watch_minutes` for a day) and daily `views` add up the views
  that started on that date.
- **Session-level statistics are credited to the date the session started.** `sessions`,
  `longest_session_min`, `avg_session_minutes` and `binge_sessions` for a day count only
  sessions that **started** that day, with their **full** length.

### Worked example

One session (session gap 30 min, cap 180 s), Friday 6 March to Saturday 7 March:

| View | Local time | Date | Estimate |
|---|---|---|---|
| 1 | Fri 23:58:00 | 2026-03-06 | 90 s (gap to next) |
| 2 | Fri 23:59:30 | 2026-03-06 | 60 s |
| 3 | Sat 00:00:30 | 2026-03-07 | 30 s |
| 4 | Sat 00:01:00 | 2026-03-07 | last: median(90, 60, 30) = 60 s |

- Session total: 90 + 60 + 30 + 60 = **240 s (4.0 min)**.
- **Friday** gets 90 + 60 = **150 s** of watch time and 2 views. It also gets the session:
  `sessions` +1, and a `longest_session_min` of at least **4.0** (the whole session).
- **Saturday** gets 30 + 60 = **90 s** of watch time, 2 views and 2 late-night views, but
  **0 sessions** and a `longest_session_min` of 0 from this session.

So summing daily watch minutes over any range always equals the total of the views in it,
while a session is never counted twice or split.

## Coverage

The date spine (`dim_date`) runs from the first to the last date with any event in the loaded
exports. Days after the latest export are **not** zero-use days; they are unknown and are not
in the tables.
