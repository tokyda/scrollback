-- One row per search, with the normalised term only.
select
    search_id,
    search_term_norm,
    event_ts_local,
    local_date
from {{ ref('stg_search') }}
