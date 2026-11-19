-- One row per session (DESIGN §4.3-4.4).
-- local_date is the date the session STARTED; a session crossing midnight belongs to its
-- start date here, while fct_views credits each view to its own date.
-- end_ts_local is the observed start of the last view, not an estimated end.
select
    session_id,
    arg_min(event_ts_local, event_ts_utc) as start_ts_local,
    arg_max(event_ts_local, event_ts_utc) as end_ts_local,
    cast(arg_min(event_ts_local, event_ts_utc) as date) as local_date,
    count(*) as n_views,
    sum(est_watch_sec) as est_watch_sec,
    sum(est_watch_sec) >= {{ var('binge_threshold_min') }} * 60 as is_binge
from {{ ref('fct_views') }}
group by session_id
