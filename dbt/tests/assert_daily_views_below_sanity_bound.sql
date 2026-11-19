-- No day may have more views than var('max_daily_views'); more suggests a parsing bug.
select local_date, views
from {{ ref('agg_daily') }}
where views > {{ var('max_daily_views') }}
