-- Session totals equal the sum of their views' estimates and view counts.
select s.session_id
from {{ ref('fct_sessions') }} as s
inner join (
    select session_id, count(*) as n_views, sum(est_watch_sec) as est_watch_sec
    from {{ ref('fct_views') }}
    group by session_id
) as v using (session_id)
where s.n_views != v.n_views or abs(s.est_watch_sec - v.est_watch_sec) > 1e-9
