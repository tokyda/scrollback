-- Warn (not fail) when the latest event is older than var('freshness_warn_days') days.
{{ config(severity='warn') }}
select max(local_date) as last_date
from {{ ref('dim_date') }}
having max(local_date) < {{ local_today() }} - {{ var('freshness_warn_days') }}
