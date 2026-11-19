-- One row per view with its estimated watch seconds (DESIGN §4.3).
--   not last in session: least(gap to next view, view_cap_sec)
--   last in session:     median of the other views' estimates in the same session,
--                        or default_last_view_sec when the session has a single view.
-- median() averages the two middle values for an even count, so estimates can end in .5.
with sessions as (
    select
        *,
        view_seq_in_session = session_n_views as is_last_in_session
    from {{ ref('int_sessions') }}
),

non_last_estimates as (
    select
        *,
        case
            when not is_last_in_session
                then cast(least(gap_sec, {{ var('view_cap_sec') }}) as double)
        end as est_non_last_sec
    from sessions
),

session_medians as (
    -- median() ignores nulls, i.e. the last view's own (null) estimate.
    select session_id, median(est_non_last_sec) as median_other_sec
    from non_last_estimates
    group by session_id
)

select
    v.view_id,
    v.video_id,
    v.session_id,
    v.event_ts_utc,
    v.event_ts_local,
    v.local_date,
    v.local_hour,
    v.dow,
    v.dow in (6, 7) as is_weekend,
    v.local_hour >= {{ var('late_night_start_hour') }}
        and v.local_hour < {{ var('late_night_end_hour') }} as is_late_night,
    v.gap_sec as gap_to_next_sec,
    v.view_seq_in_session,
    v.is_last_in_session,
    cast(
        case
            when not v.is_last_in_session then v.est_non_last_sec
            else coalesce(m.median_other_sec, {{ var('default_last_view_sec') }})
        end as double
    ) as est_watch_sec,
    -- A rewatch is any view of a video already viewed earlier in the loaded history.
    row_number() over (
        partition by v.video_id order by v.event_ts_utc, v.view_id
    ) > 1 as is_rewatch
from non_last_estimates as v
inner join session_medians as m using (session_id)
