-- One row per view event, typed and converted to UTC and local time.
-- Raw rows are already unique by record_hash (ingest dedupes), so record_hash is the view_id.
with source as (
    select record_hash, video_id, {{ parse_export_ts('event_ts_raw') }} as event_ts_source
    from {{ source('raw', 'raw_watch') }}
),

converted as (
    select
        record_hash as view_id,
        video_id,
        {{ source_to_utc('event_ts_source') }} as event_ts_utc,
        {{ source_to_local('event_ts_source') }} as event_ts_local
    from source
)

select
    view_id,
    video_id,
    event_ts_utc,
    event_ts_local,
    cast(event_ts_local as date) as local_date,
    hour(event_ts_local) as local_hour,
    isodow(event_ts_local) as dow  -- ISO: 1 = Monday ... 7 = Sunday
from converted
