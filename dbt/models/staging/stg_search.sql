-- One row per search. search_term_norm: NFC, lower-case, trimmed, internal whitespace collapsed.
-- (Ingest dedupes with Python's NFKC + casefold; the two agree for ordinary text.)
with source as (
    select record_hash, search_term, {{ parse_export_ts('event_ts_raw') }} as event_ts_source
    from {{ source('raw', 'raw_search') }}
)

select
    record_hash as search_id,
    search_term,
    regexp_replace(trim(lower(nfc_normalize(search_term))), '\s+', ' ', 'g') as search_term_norm,
    {{ source_to_utc('event_ts_source') }} as event_ts_utc,
    {{ source_to_local('event_ts_source') }} as event_ts_local,
    cast({{ source_to_local('event_ts_source') }} as date) as local_date
from source
