-- One row per share. video_id is null for non-video shares (e.g. a profile link).
with source as (
    select
        record_hash, video_id, method, shared_content,
        {{ parse_export_ts('event_ts_raw') }} as event_ts_source
    from {{ source('raw', 'raw_share') }}
)

select
    record_hash as share_id,
    video_id,
    method,
    shared_content,
    {{ source_to_utc('event_ts_source') }} as event_ts_utc,
    {{ source_to_local('event_ts_source') }} as event_ts_local,
    cast({{ source_to_local('event_ts_source') }} as date) as local_date
from source
