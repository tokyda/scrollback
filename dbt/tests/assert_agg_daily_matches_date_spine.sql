-- agg_daily has exactly one row per day of the dim_date span (no gaps, no extras).
with spine as (
    select count(*) as n, min(local_date) as first_date, max(local_date) as last_date
    from {{ ref('dim_date') }}
),
agg as (select count(*) as n from {{ ref('agg_daily') }})
select spine.n as spine_rows, agg.n as agg_rows
from spine, agg
where agg.n != spine.n
   or (spine.n > 0 and spine.n != date_diff('day', spine.first_date, spine.last_date) + 1)
