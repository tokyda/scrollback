-- Each view with the time to the next and from the previous view (DESIGN §4.3).
-- Gaps are measured in UTC so daylight-saving changes in local_tz can't distort them.
-- Ties (two views in the same second) are ordered by view_id so results are deterministic.
select
    view_id,
    video_id,
    event_ts_utc,
    event_ts_local,
    local_date,
    local_hour,
    dow,
    lead(event_ts_utc) over w as next_event_ts_utc,
    date_diff('second', event_ts_utc, lead(event_ts_utc) over w) as gap_sec,
    date_diff('second', lag(event_ts_utc) over w, event_ts_utc) as gap_from_prev_sec
from {{ ref('stg_watch') }}
window w as (order by event_ts_utc, view_id)
