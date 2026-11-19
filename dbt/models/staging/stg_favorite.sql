-- One row per favorite event, typed and converted to UTC and local time.
with source as (
    select record_hash, video_id, {{ parse_export_ts('event_ts_raw') }} as event_ts_source
    from {{ source('raw', 'raw_favorite') }}
)

select
    record_hash as favorite_id,
    video_id,
    {{ source_to_utc('event_ts_source') }} as event_ts_utc,
    {{ source_to_local('event_ts_source') }} as event_ts_local,
    cast({{ source_to_local('event_ts_source') }} as date) as local_date
from source
