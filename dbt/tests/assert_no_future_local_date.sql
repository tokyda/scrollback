-- No event may be dated after today in the local timezone.
select local_date
from {{ ref('dim_date') }}
where local_date > {{ local_today() }}
