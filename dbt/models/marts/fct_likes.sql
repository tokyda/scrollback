-- One row per like. liked_after_watch: a view of the same video happened at or before the
-- like and no more than var('session_gap_min') minutes earlier.
select
    l.like_id,
    l.video_id,
    l.event_ts_local,
    l.local_date,
    exists (
        select 1
        from {{ ref('stg_watch') }} as w
        where w.video_id = l.video_id
          and w.event_ts_utc <= l.event_ts_utc
          and w.event_ts_utc >= l.event_ts_utc - to_minutes({{ var('session_gap_min') }})
    ) as liked_after_watch
from {{ ref('stg_like') }} as l
