-- Date spine over the watch-history window: the first to the last local date with a view.
-- Days outside it are unknown, not zero: before the window the export has no watch history
-- (likes and favourites can reach back years), after it the export hasn't happened yet.
-- Likes and searches outside the window stay in their fact tables.
-- Weeks run Monday-Sunday (ISO).
with bounds as (
    select min(local_date) as first_date, max(local_date) as last_date
    from {{ ref('stg_watch') }}
),

spine as (
    select cast(d as date) as local_date
    from bounds, generate_series(first_date, last_date, interval 1 day) as t(d)
    where first_date is not null
)

select
    local_date,
    isodow(local_date) as dow,
    dayname(local_date) as dow_name,
    isodow(local_date) in (6, 7) as is_weekend,
    strftime(local_date, '%G-W%V') as iso_week,
    cast(date_trunc('week', local_date) as date) as week_start,
    month(local_date) as month,
    cast(date_trunc('month', local_date) as date) as month_start,
    year(local_date) as year
from spine
