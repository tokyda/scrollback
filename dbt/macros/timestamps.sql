{#
  Export timestamps are naive and in var('source_tz') (DESIGN §4.1).
  These macros turn a naive source timestamp into naive UTC or naive local (var('local_tz')).
#}
{% macro parse_export_ts(column) -%}
    strptime({{ column }}, '%Y-%m-%d %H:%M:%S')
{%- endmacro %}

{% macro source_to_utc(ts) -%}
    timezone('UTC', timezone('{{ var("source_tz") }}', {{ ts }}))
{%- endmacro %}

{% macro source_to_local(ts) -%}
    timezone('{{ var("local_tz") }}', timezone('{{ var("source_tz") }}', {{ ts }}))
{%- endmacro %}

{# Today's date in the local timezone. #}
{% macro local_today() -%}
    cast(timezone('{{ var("local_tz") }}', now()) as date)
{%- endmacro %}
