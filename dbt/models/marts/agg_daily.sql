-- One row per dim_date day; days without activity are 0, not missing.
-- View-level numbers (views, est_watch_min, late_night_views) use each view's own date.
-- Session-level numbers (sessions, longest_session_min) use the session's START date.
with views as (
    select
        local_date,
        count(*) as views,
        sum(est_watch_sec) / 60.0 as est_watch_min,
        count(*) filter (where is_late_night) as late_night_views
    from {{ ref('fct_views') }}
    group by local_date
),

sessions as (
    select
        local_date,
        count(*) as sessions,
        max(est_watch_sec) / 60.0 as longest_session_min
    from {{ ref('fct_sessions') }}
    group by local_date
),

likes as (
    select local_date, count(*) as likes from {{ ref('fct_likes') }} group by local_date
),

searches as (
    select local_date, count(*) as searches from {{ ref('fct_searches') }} group by local_date
),

daily as (
    select
        d.local_date,
        d.dow,
        d.is_weekend,
        coalesce(v.views, 0) as views,
        coalesce(v.est_watch_min, 0.0) as est_watch_min,
        coalesce(s.sessions, 0) as sessions,
        coalesce(s.longest_session_min, 0.0) as longest_session_min,
        coalesce(v.late_night_views, 0) as late_night_views,
        coalesce(l.likes, 0) as likes,
        coalesce(q.searches, 0) as searches,
        coalesce(v.views, 0) > 0 as is_active
    from {{ ref('dim_date') }} as d
    left join views as v using (local_date)
    left join sessions as s using (local_date)
    left join likes as l using (local_date)
    left join searches as q using (local_date)
)

select
    *,
    -- Active days in the trailing 7-day window ending on this date (the spine is contiguous,
    -- so 6 preceding rows = 6 preceding days). Early days in the spine see a shorter window.
    sum(case when is_active then 1 else 0 end) over (
        order by local_date rows between 6 preceding and current row
    ) as active_days_7d
from daily
