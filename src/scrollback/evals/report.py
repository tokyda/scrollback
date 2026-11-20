"""Summaries and markdown reports for eval runs (DESIGN §7.4).

Accuracy is the share of (question) runs that pass, computed per repeat and reported as
mean with min-max spread across repeats. Per-category accuracy is shown for the dev split
only; the test split is reported overall (too few questions per category to read into).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean
from typing import Any

from scrollback.evals.questions import CATEGORIES, QuestionSet


def _rate(values: list[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def _spread(per_repeat: list[float]) -> dict[str, float | None]:
    if not per_repeat:
        return {"mean": None, "min": None, "max": None}
    return {"mean": mean(per_repeat), "min": min(per_repeat), "max": max(per_repeat)}


def _per_repeat_rate(records: list[dict[str, Any]], key: str = "passed") -> list[float]:
    by_rep: dict[int, list[bool]] = defaultdict(list)
    for r in records:
        by_rep[r["repeat"]].append(bool(r.get(key)))
    return [sum(v) / len(v) for _, v in sorted(by_rep.items()) if v]


def _sub_rate(records: list[dict[str, Any]], name: str) -> float | None:
    vals = [r["scores"][name] for r in records if r.get("scores", {}).get(name) is not None]
    return _rate([bool(v) for v in vals])


def summarize(records: list[dict[str, Any]], qset: QuestionSet) -> dict[str, Any]:
    repeats = sorted({r["repeat"] for r in records})
    dev = [r for r in records if r["split"] == "dev"]
    test = [r for r in records if r["split"] == "test"]
    per_question: dict[str, list[bool]] = defaultdict(list)
    for r in records:
        per_question[r["question"]].append(bool(r["passed"]))
    costs = [r["cost_usd"] for r in records if isinstance(r.get("cost_usd"), int | float)]
    caveat_qs = {q.id for q in qset.questions if q.requires_caveat}
    est_qs = {
        q.id for q in qset.questions
        if set(q.expected_metrics) & {"est_watch_minutes", "avg_session_minutes", "binge_sessions"}
    }  # fmt: skip
    return {
        "repeats": len(repeats),
        "n_questions": len(per_question),
        "n_runs": len(records),
        "accuracy": {
            "overall": _spread(_per_repeat_rate(records)),
            "dev": _spread(_per_repeat_rate(dev)),
            "test": _spread(_per_repeat_rate(test)),
            "dev_by_category": {
                c: _spread(_per_repeat_rate([r for r in dev if r["category"] == c]))
                for c in CATEGORIES
                if any(r["category"] == c for r in dev)
            },
        },
        "sub_scores": {
            "execution": _sub_rate(records, "execution"),
            "answer": _sub_rate(records, "answer"),
            "metric_selection": _sub_rate(records, "metric_selection"),
            "refusal_correct": _sub_rate(
                [r for r in records if r["category"] == "unanswerable"], "refusal"
            ),
            "caveat_on_traps": _sub_rate(
                [r for r in records if r["question"] in caveat_qs], "caveat"
            ),
            "caveat_on_estimate_questions": _sub_rate(
                [r for r in records if r["question"] in est_qs], "caveat"
            ),
            "assumptions_on_ambiguous": _sub_rate(
                [r for r in records if r["category"] == "ambiguous"], "assumptions"
            ),
        },
        "efficiency": {
            "steps": mean(r.get("steps", 0) for r in records) if records else None,
            "tool_calls": mean(r.get("tool_calls", 0) for r in records) if records else None,
            "tool_errors": mean(r.get("tool_errors", 0) for r in records) if records else None,
            "tokens": mean(sum((r.get("tokens") or {}).values()) for r in records)
            if records
            else None,
            "latency_s": mean(r.get("latency_ms", 0) for r in records) / 1000 if records else None,
            "cost_per_question_usd": mean(costs) if costs else None,
            "total_cost_usd": sum(costs) if costs else None,
        },
        "statuses": dict(Counter(r["status"] for r in records)),
        "labels_suggested": dict(
            Counter(r["label_suggested"] for r in records if not r["passed"]).most_common()
        ),
        "suspect_scoring": sum(1 for r in records if r.get("suspect_scoring")),
        "per_question": {qid: f"{sum(v)}/{len(v)}" for qid, v in sorted(per_question.items())},
        "flaky": sorted(q for q, v in per_question.items() if 0 < sum(v) < len(v)),
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _acc(cell: dict[str, float | None]) -> str:
    if cell["mean"] is None:
        return "n/a"
    lo, hi = cell["min"], cell["max"]
    assert lo is not None and hi is not None
    return f"{_pct(cell['mean'])} ({_pct(lo)}-{_pct(hi)})"


def render_report(
    summary: dict[str, Any],
    records: list[dict[str, Any]],
    qset: QuestionSet,
    confirmed: dict[tuple[str, int], dict[str, Any]] | None = None,
) -> str:
    acc = summary["accuracy"]
    eff = summary["efficiency"]
    lines = [
        f"# Eval report: {summary.get('run_id', '')}",
        "",
        f"{summary['n_questions']} questions x {summary['repeats']} repeats = "
        f"{summary['n_runs']} runs. Accuracy is mean (min-max across repeats).",
        "",
        "## Accuracy",
        "",
        "| Slice | Accuracy |",
        "|---|---|",
        f"| **Overall** | {_acc(acc['overall'])} |",
        f"| Dev | {_acc(acc['dev'])} |",
        f"| Test (held out; overall only) | {_acc(acc['test'])} |",
        "",
        "### Dev, by category",
        "",
        "| Category | Accuracy |",
        "|---|---|",
    ]
    for cat, cell in acc["dev_by_category"].items():
        lines.append(f"| {cat} | {_acc(cell)} |")
    sub = summary["sub_scores"]
    lines += [
        "",
        "## Sub-scores (all runs)",
        "",
        "| Score | Rate |",
        "|---|---|",
        f"| execution_accuracy | {_pct(sub['execution'])} |",
        f"| answer_accuracy | {_pct(sub['answer'])} |",
        f"| metric_selection (informational in E0) | {_pct(sub['metric_selection'])} |",
        f"| refusal_correct (unanswerable) | {_pct(sub['refusal_correct'])} |",
        f"| caveat_present (traps) | {_pct(sub['caveat_on_traps'])} |",
        "| caveat_present (all estimate questions) | "
        f"{_pct(sub['caveat_on_estimate_questions'])} |",
        f"| assumptions stated (ambiguous) | {_pct(sub['assumptions_on_ambiguous'])} |",
        "",
        "## Efficiency (mean per question run)",
        "",
        f"- steps {eff['steps']:.2f}, tool calls {eff['tool_calls']:.2f}, "
        f"tool errors {eff['tool_errors']:.2f}, tokens {eff['tokens']:,.0f}, "
        f"latency {eff['latency_s']:.1f}s",
        f"- cost ${eff['cost_per_question_usd'] or 0:.4f} per question run, "
        f"**total ${eff['total_cost_usd'] or 0:.2f}**",
        f"- statuses: {summary['statuses']}",
        "",
        "## Bad cases",
        "",
    ]
    failures = [r for r in records if not r["passed"]]
    labels: Counter[str] = Counter()
    for r in failures:
        entry = (confirmed or {}).get((r["question"], r["repeat"]))
        labels[entry["label"] if entry else f"{r['label_suggested']} (suggested)"] += 1
    lines += ["| Label | Failures |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in labels.most_common()]
    lines += ["", "### Failures", "", "| Q | Rep | Split | Category | Label | Reason |",
              "|---|---|---|---|---|---|"]  # fmt: skip
    for r in failures:
        entry = (confirmed or {}).get((r["question"], r["repeat"]))
        label = entry["label"] if entry else f"{r['label_suggested']}*"
        reason = (r.get("label_reason") or "").replace("|", "/")
        if r.get("suspect_scoring"):
            reason += f" — suspect scoring: {r['suspect_scoring']}"
        lines.append(
            f"| {r['question']} | {r['repeat']} | {r['split']} | {r['category']} | {label} | "
            f"{reason} |"
        )
    lines += [
        "",
        "`*` = suggested label, not yet confirmed.",
        "",
        "## Per question (passes / repeats)",
        "",
        ", ".join(f"{q} {v}" for q, v in summary["per_question"].items()),
        "",
        f"Flaky (passed some repeats only): {', '.join(summary['flaky']) or 'none'}",
        "",
    ]
    return "\n".join(lines)


def compare(a: dict[str, Any], b: dict[str, Any]) -> str:
    """Markdown diff of two summaries: accuracy deltas, regressions and improvements."""
    lines = [
        f"# Compare {a.get('run_id')} -> {b.get('run_id')}",
        "",
        "| Slice | A | B |",
        "|---|---|---|",
    ]
    for key in ("overall", "dev", "test"):
        lines.append(f"| {key} | {_acc(a['accuracy'][key])} | {_acc(b['accuracy'][key])} |")
    for cat in CATEGORIES:
        ca = a["accuracy"]["dev_by_category"].get(cat)
        cb = b["accuracy"]["dev_by_category"].get(cat)
        if ca or cb:
            na: dict[str, float | None] = {"mean": None, "min": None, "max": None}
            lines.append(f"| dev/{cat} | {_acc(ca or na)} | {_acc(cb or na)} |")

    def frac(v: str) -> float:
        k, n = v.split("/")
        return int(k) / int(n)

    pa, pb = a["per_question"], b["per_question"]
    common = sorted(set(pa) & set(pb))
    worse = [q for q in common if frac(pb[q]) < frac(pa[q])]
    better = [q for q in common if frac(pb[q]) > frac(pa[q])]
    lines += [
        "",
        f"Regressed ({len(worse)}): "
        + (", ".join(f"{q} {pa[q]}->{pb[q]}" for q in worse) or "none"),
        f"Improved ({len(better)}): "
        + (", ".join(f"{q} {pa[q]}->{pb[q]}" for q in better) or "none"),
        "",
        f"Cost: ${a['efficiency']['total_cost_usd'] or 0:.2f} -> "
        f"${b['efficiency']['total_cost_usd'] or 0:.2f}",
    ]
    return "\n".join(lines)
