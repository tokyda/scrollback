-- Session assignment (DESIGN §4.3).
-- A view starts a new session when it is the first view, or when the gap from the previous
-- view is greater than var('session_gap_min') minutes. A gap exactly equal to the threshold
-- stays in the same session. A running sum of the "new session" flags numbers the sessions;
-- session_id is the view_id of the session's first view, so it is stable across rebuilds.
with flagged as (
    select
        *,
        case
            when gap_from_prev_sec is null then 1
            when gap_from_prev_sec > {{ var('session_gap_min') }} * 60 then 1
            else 0
        end as is_session_start
    from {{ ref('int_view_gaps') }}
),

numbered as (
    select
        *,
        sum(is_session_start) over (
            order by event_ts_utc, view_id rows between unbounded preceding and current row
        ) as session_seq
    from flagged
)

select
    *,
    first_value(view_id) over (
        partition by session_seq order by event_ts_utc, view_id
    ) as session_id,
    row_number() over (
        partition by session_seq order by event_ts_utc, view_id
    ) as view_seq_in_session,
    count(*) over (partition by session_seq) as session_n_views
from numbered
