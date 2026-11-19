"""Distribution of gaps between consecutive views, for tuning parameters (DESIGN §4.3).

Everything here is an aggregate: counts, percentiles, histogram bins. No timestamps, video
IDs or individual rows are returned or printed, so it is safe to run on a real export.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

PERCENTILES: tuple[float, ...] = (0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)
BASE_EDGES_SEC: tuple[int, ...] = (
    0, 5, 10, 20, 30, 60, 120, 180, 300, 600, 1200, 1800, 3600, 7200, 21600, 86400,
)  # fmt: skip
WHAT_IF_SESSION_GAP_MIN: tuple[int, ...] = (5, 10, 15, 20, 30, 45, 60, 90)
WHAT_IF_CAP_SEC: tuple[int, ...] = (60, 120, 180, 300, 600)
BAR_WIDTH = 40


class WarehouseNotBuiltError(RuntimeError):
    """int_view_gaps does not exist yet."""


@dataclass(frozen=True)
class Bin:
    low_sec: int
    high_sec: int | None  # None = open-ended
    count: int


@dataclass(frozen=True)
class GapStats:
    n_views: int
    n_gaps: int
    percentiles: dict[float, float]
    within_session_percentiles: dict[float, float]
    bins: list[Bin]
    view_cap_sec: int
    session_gap_sec: int
    sessions_by_gap_min: dict[int, int]  # what-if: session count per session_gap_min
    capped_share_by_cap: dict[int, float]  # what-if: share of within-session gaps > cap


def _scalar(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _quantiles(con: duckdb.DuckDBPyConnection, where: str) -> dict[float, float]:
    row = con.execute(
        f"SELECT quantile_cont(gap_sec, {list(PERCENTILES)}) FROM int_view_gaps "
        f"WHERE gap_sec IS NOT NULL {where}"
    ).fetchone()
    values = row[0] if row else None
    if not values:
        return {}
    return dict(zip(PERCENTILES, (float(v) for v in values), strict=True))


def gap_stats(con: duckdb.DuckDBPyConnection, view_cap_sec: int, session_gap_min: int) -> GapStats:
    exists = _scalar(
        con, "SELECT count(*) FROM information_schema.tables WHERE table_name = 'int_view_gaps'"
    )
    if not exists:
        raise WarehouseNotBuiltError("int_view_gaps not found: run `scrollback build` first")

    session_gap_sec = session_gap_min * 60
    n_views = _scalar(con, "SELECT count(*) FROM int_view_gaps")
    n_gaps = _scalar(con, "SELECT count(gap_sec) FROM int_view_gaps")

    edges = sorted({*BASE_EDGES_SEC, view_cap_sec, session_gap_sec})
    ranges: list[tuple[int, int | None]] = [
        (lo, edges[i + 1] if i + 1 < len(edges) else None) for i, lo in enumerate(edges)
    ]
    filters = [
        f"count(*) FILTER (WHERE gap_sec >= {lo}"
        + (f" AND gap_sec < {hi})" if hi is not None else ")")
        for lo, hi in ranges
    ]
    row = con.execute(
        f"SELECT {', '.join(filters)} FROM int_view_gaps WHERE gap_sec IS NOT NULL"
    ).fetchone()
    counts = list(row) if row else [0] * len(ranges)
    bins = [Bin(lo, hi, int(c)) for (lo, hi), c in zip(ranges, counts, strict=True)]

    sessions_by_gap = {
        g: (1 + _scalar(con, f"SELECT count(*) FROM int_view_gaps WHERE gap_sec > {g * 60}"))
        if n_views
        else 0
        for g in sorted({*WHAT_IF_SESSION_GAP_MIN, session_gap_min})
    }
    within = _scalar(con, f"SELECT count(*) FROM int_view_gaps WHERE gap_sec <= {session_gap_sec}")
    capped = {
        c: (
            _scalar(
                con,
                f"SELECT count(*) FROM int_view_gaps "
                f"WHERE gap_sec > {c} AND gap_sec <= {session_gap_sec}",
            )
            / within
            if within
            else 0.0
        )
        for c in sorted({*WHAT_IF_CAP_SEC, view_cap_sec})
    }
    return GapStats(
        n_views=n_views,
        n_gaps=n_gaps,
        percentiles=_quantiles(con, ""),
        within_session_percentiles=_quantiles(con, f"AND gap_sec <= {session_gap_sec}"),
        bins=bins,
        view_cap_sec=view_cap_sec,
        session_gap_sec=session_gap_sec,
        sessions_by_gap_min=sessions_by_gap,
        capped_share_by_cap=capped,
    )


def fmt_duration(seconds: float) -> str:
    s = round(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m" if sec == 0 else f"{m}m{sec:02d}s"
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h}h" if m == 0 else f"{h}h{m:02d}m"


def _bin_label(b: Bin) -> str:
    if b.high_sec is None:
        return f">= {fmt_duration(b.low_sec)}"
    return f"{fmt_duration(b.low_sec)} - <{fmt_duration(b.high_sec)}"


def format_gap_report(stats: GapStats) -> str:
    cap, gap = stats.view_cap_sec, stats.session_gap_sec
    lines = [
        f"Views: {stats.n_views:,}   gaps between consecutive views: {stats.n_gaps:,}",
        f"Current parameters: view_cap_sec = {cap} ({fmt_duration(cap)}), "
        f"session_gap_min = {gap // 60} ({fmt_duration(gap)})",
    ]
    if stats.n_gaps == 0:
        lines.append("Not enough views to measure gaps.")
        return "\n".join(lines)

    def pct_row(title: str, values: dict[float, float]) -> list[str]:
        if not values:
            return [f"{title}: (none)"]
        head = "".join(f"{'p' + str(round(p * 100)):>9}" for p in values)
        body = "".join(f"{fmt_duration(v):>9}" for v in values.values())
        return [title, head, body]

    lines += ["", *pct_row("Percentiles, all gaps:", stats.percentiles)]
    lines += [
        "",
        *pct_row(
            f"Percentiles, within-session gaps (<= {fmt_duration(gap)}):",
            stats.within_session_percentiles,
        ),
    ]

    lines += ["", "Histogram of gaps (each # is a share of the largest bin):"]
    biggest = max(b.count for b in stats.bins) or 1
    label_w = max(len(_bin_label(b)) for b in stats.bins)
    for b in stats.bins:
        bar = "#" * round(BAR_WIDTH * b.count / biggest)
        share = b.count / stats.n_gaps
        marks = []
        if b.high_sec == cap:
            marks.append("view_cap_sec")
        if b.high_sec == gap:
            marks.append("session_gap_min")
        mark = f"  <- up to {' & '.join(marks)}" if marks else ""
        lines.append(f"  {_bin_label(b):>{label_w}} {b.count:>8,} {share:6.1%} {bar}{mark}")

    lines += ["", "What if session_gap_min were ...  (sessions = 1 + gaps above the threshold)"]
    for g, n in stats.sessions_by_gap_min.items():
        current = "  <- current" if g * 60 == gap else ""
        lines.append(f"  {g:>4} min: {n:>8,} sessions{current}")

    lines += [
        "",
        "What if view_cap_sec were ...  (share of within-session gaps that would be capped)",
    ]
    for c, share in stats.capped_share_by_cap.items():
        current = "  <- current" if c == cap else ""
        lines.append(f"  {c:>4} s: {share:6.1%} capped{current}")
    return "\n".join(lines)
