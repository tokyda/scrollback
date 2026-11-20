"""Gold answers: run each question's gold SQL on its anchor warehouse (DESIGN §7.1).

``check_gold`` is the CI validity check: every gold (and alternative) SQL must run, return
rows unless the question says otherwise, have the declared shape, reference real metrics,
and match the committed snapshot ``eval/gold_snapshot.json`` so drift is always deliberate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from scrollback.evals.questions import Question, QuestionSet
from scrollback.semantic.metrics import load_metrics

DEFAULT_SNAPSHOT = Path(__file__).resolve().parents[3] / "eval" / "gold_snapshot.json"


@dataclass(frozen=True)
class GoldAnswer:
    columns: list[str]
    rows: list[list[Any]]

    @property
    def scalar(self) -> Any:
        return self.rows[0][0] if self.rows and self.rows[0] else None


@dataclass
class QuestionGold:
    gold: GoldAnswer | None
    alternatives: list[GoldAnswer] = field(default_factory=list)

    def all(self) -> list[GoldAnswer]:
        return ([self.gold] if self.gold else []) + self.alternatives

    def scalar_value(self) -> Any:
        return self.gold.scalar if self.gold else None


def jsonable(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float):
        return round(value, 6)
    return value


def run_gold_sql(con: duckdb.DuckDBPyConnection, sql: str) -> GoldAnswer:
    cursor = con.execute(sql)
    rows = cursor.fetchall()
    columns = [d[0] for d in cursor.description or []]
    return GoldAnswer(columns, [[jsonable(v) for v in r] for r in rows])


def compute_golds(qset: QuestionSet, warehouses: dict[date, Path]) -> dict[str, QuestionGold]:
    golds: dict[str, QuestionGold] = {}
    cons = {d: duckdb.connect(str(p), read_only=True) for d, p in warehouses.items()}
    try:
        for q in qset.questions:
            if q.answer_type in ("refusal", "no_data"):
                golds[q.id] = QuestionGold(None)
                continue
            con = cons[q.today]
            assert q.gold_sql is not None
            golds[q.id] = QuestionGold(
                run_gold_sql(con, q.gold_sql), [run_gold_sql(con, a) for a in q.alternatives]
            )
    finally:
        for con in cons.values():
            con.close()
    return golds


def _shape_problems(q: Question, answer: GoldAnswer, label: str) -> list[str]:
    problems = []
    if not answer.rows and not q.expect_empty:
        problems.append(f"{q.id}: {label} returned no rows")
    if q.answer_type == "scalar" and answer.rows:
        if len(answer.rows) != 1:
            problems.append(f"{q.id}: {label} scalar returned {len(answer.rows)} rows")
        if answer.scalar is None and not q.expect_empty:
            problems.append(f"{q.id}: {label} scalar is NULL")
    if q.answer_type in ("table", "ordered_table") and len(answer.columns) != len(q.units):
        problems.append(
            f"{q.id}: {label} has {len(answer.columns)} columns but {len(q.units)} units"
        )
    return problems


def check_gold(qset: QuestionSet, golds: dict[str, QuestionGold]) -> list[str]:
    from scrollback.agent.sql_guard import SqlGuardError, guard_sql

    metric_names = {m.name for m in load_metrics().metrics}
    problems: list[str] = []
    for q in qset.questions:
        # Every gold must be expressible by the agent under aggregate_only.
        for sql in ([q.gold_sql] if q.gold_sql else []) + q.alternatives:
            try:
                guard_sql(sql, result_mode="aggregate_only", max_rows=200)
            except SqlGuardError as exc:
                problems.append(f"{q.id}: gold SQL rejected by the agent's guard ({exc.rule})")
        unknown = sorted(set(q.expected_metrics) - metric_names)
        if unknown:
            problems.append(f"{q.id}: unknown expected_metrics {unknown}")
        if q.answer_type == "ordered_table" and "order by" not in (q.gold_sql or "").lower():
            problems.append(f"{q.id}: ordered_table gold SQL needs ORDER BY")
        g = golds[q.id]
        if g.gold is not None:
            problems += _shape_problems(q, g.gold, "gold")
            for i, alt in enumerate(g.alternatives, 1):
                problems += _shape_problems(q, alt, f"alternative {i}")
            if q.answer_type == "scalar" and isinstance(g.scalar_value(), int | float):
                value = float(g.scalar_value())
                for wrong in q.wrong_values:
                    if abs(wrong - value) <= max(q.tolerance, 1e-9):
                        problems.append(f"{q.id}: wrong_value {wrong} equals the gold answer")
    return problems


def snapshot(golds: dict[str, QuestionGold]) -> dict[str, Any]:
    return {
        qid: {
            "gold": None if g.gold is None else {"columns": g.gold.columns, "rows": g.gold.rows},
            "alternatives": [{"columns": a.columns, "rows": a.rows} for a in g.alternatives],
        }
        for qid, g in sorted(golds.items())
    }


def write_snapshot(golds: dict[str, QuestionGold], path: Path = DEFAULT_SNAPSHOT) -> None:
    path.write_text(json.dumps(snapshot(golds), indent=1, ensure_ascii=False) + "\n")


def snapshot_drift(golds: dict[str, QuestionGold], path: Path = DEFAULT_SNAPSHOT) -> list[str]:
    if not path.exists():
        return [f"no gold snapshot at {path.name}; run `scrollback eval check-gold --update`"]
    committed = json.loads(path.read_text(encoding="utf-8"))
    current = json.loads(json.dumps(snapshot(golds)))
    drift = []
    for qid in sorted(set(committed) | set(current)):
        if committed.get(qid) != current.get(qid):
            drift.append(f"{qid}: gold differs from snapshot")
    return drift
