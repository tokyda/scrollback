"""Run an experiment over the eval set (DESIGN §7.4).

    scrollback eval run --config eval/experiments/e00_baseline.yaml --split all --repeat 3

Each (question, repeat) runs the agent against the question's anchor warehouse with the set's
pinned parameters and ``today``, then is scored and, on failure, given a suggested label.
Artifacts go to ``eval/runs/<UTC timestamp>_<name>/``:

    config.json     resolved non-secret config, experiment, split, repeats, git commit
    results.jsonl   one record per (question, repeat): scores, answer, SQL, label, cost
    traces.jsonl    the full agent trace for every run
    summary.json    aggregated scores (committed for selected runs)
    report.md       human-readable report

Without ``--yes`` the CLI only prints a cost estimate (no API calls).
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from scrollback.agent.llm import LLMClient
from scrollback.agent.loop import config_snapshot, load_system_prompt, prompt_sha256, run_agent
from scrollback.agent.pricing import PRICES
from scrollback.config import Settings
from scrollback.evals.gold import QuestionGold, compute_golds
from scrollback.evals.labels import suggest_label, suspect_scoring, write_label_file
from scrollback.evals.questions import Question, QuestionSet
from scrollback.evals.score import AgentOutput, ResultTable, describe_table_values, score
from scrollback.evals.warehouses import build_anchors, eval_settings

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RUNS_DIR = REPO_ROOT / "eval" / "runs"
DEFAULT_LABELS_DIR = REPO_ROOT / "eval" / "labels"
RESULT_ROWS_KEPT = 200  # = agent.max_result_rows, so re-scoring sees full results


class Experiment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9_]+$")
    description: str
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)  # llm / agent sections


def load_experiment(path: Path) -> Experiment:
    return Experiment.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def experiment_settings(base: Settings, exp: Experiment, qset: QuestionSet) -> Settings:
    """Base settings + experiment overrides (llm/agent only) + the set's pinned params."""
    allowed = {"llm", "agent"}
    bad = set(exp.overrides) - allowed
    if bad:
        raise ValueError(f"experiment may only override {sorted(allowed)}, got {sorted(bad)}")
    settings = base.model_copy(deep=True)
    if "llm" in exp.overrides:
        settings.llm = type(settings.llm).model_validate(
            {**settings.llm.model_dump(), **exp.overrides["llm"]}
        )
    if "agent" in exp.overrides:
        settings.agent = type(settings.agent).model_validate(
            {**settings.agent.model_dump(), **exp.overrides["agent"]}
        )
    return eval_settings(settings, qset.params)


# --------------------------------------------------------------------------- cost estimate


@dataclass
class CostEstimate:
    runs: int
    per_run_usd: float
    total_usd: float
    high_usd: float  # p90-based
    basis: str


def estimate_cost(
    runs: int, model: str, history: list[Path] | None = None, harder: float = 1.3
) -> CostEstimate:
    """Estimate from past traces of the same model, scaled for the harder eval set."""
    costs: list[float] = []
    for path in history or []:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("model") == model and isinstance(rec.get("cost_usd"), int | float):
                costs.append(float(rec["cost_usd"]))
    if costs:
        costs.sort()
        per = mean(costs) * harder
        p90 = costs[min(len(costs) - 1, int(0.9 * len(costs)))] * harder
        basis = f"{len(costs)} past traces of {model}, mean ${mean(costs):.4f} x{harder} harder"
    else:
        price = PRICES.get(model)
        if price is None:
            return CostEstimate(runs, 0.0, 0.0, 0.0, f"no price known for {model}")
        # ~4 steps: 3k uncached input, 12k cache reads, 4k cache writes, 1k output per question
        per = (3000 * price.input + 12000 * price.cache_read + 4000 * price.cache_write
               + 1000 * price.output) / 1e6  # fmt: skip
        p90 = per * 2
        basis = "token assumption (no past traces)"
    return CostEstimate(runs, per, per * runs, p90 * runs, basis)


# --------------------------------------------------------------------------- running


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True,
            text=True, check=True,
        )  # fmt: skip
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 6)
    return value


def run_one(
    q: Question,
    gold: QuestionGold,
    repeat: int,
    *,
    settings: Settings,
    llm: LLMClient,
    db_path: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run, score and label one (question, repeat). Returns (record, trace dict)."""
    started = time.perf_counter()
    try:
        result = run_agent(
            q.question, settings=settings, llm=llm, db_path=db_path, today=q.today,
            write_traces=False,
        )  # fmt: skip
    except Exception as exc:  # keep the run going; record the harness failure
        record = {
            "question": q.id, "repeat": repeat, "category": q.category, "split": q.split,
            "difficulty": q.difficulty, "status": "harness_error", "passed": False,
            "error": f"{type(exc).__name__}: {exc}", "label_suggested": "tool_loop_or_step_limit",
            "label_reason": "harness error", "latency_ms": (time.perf_counter() - started) * 1000,
        }  # fmt: skip
        return record, None

    answer = result.answer
    cited = {}
    if answer:
        for rid in answer.result_ids:
            if rid in result.results:
                r = result.results[rid]
                cited[rid] = ResultTable(r.columns, [[_jsonable(v) for v in row] for row in r.rows])
    all_results = {
        rid: ResultTable(r.columns, [[_jsonable(v) for v in row] for row in r.rows])
        for rid, r in result.results.items()
    }
    out = AgentOutput(
        status=result.status,
        text=answer.text if answer else "",
        result_ids=answer.result_ids if answer else [],
        metrics_used=answer.metrics_used if answer else [],
        assumptions=answer.assumptions if answer else [],
        results=cited,
        all_results=all_results,
        tool_values=describe_table_values([asdict(t) for t in result.trace.tool_calls]),
    )
    s = score(q, gold, out)
    trace = result.trace
    tool_calls = [asdict(t) for t in trace.tool_calls]
    sql_used = answer.sql_used if answer else []
    label, reason, suspect = None, None, None
    if not s.passed:
        label, reason = suggest_label(
            q, s, status=result.status, tool_calls=tool_calls, sql_used=sql_used
        )
        suspect = suspect_scoring(q, s)
    tokens = trace.tokens()
    record = {
        "question": q.id,
        "repeat": repeat,
        "category": q.category,
        "split": q.split,
        "difficulty": q.difficulty,
        "today": q.today.isoformat(),
        "status": result.status,
        "passed": s.passed,
        "scores": _scores_dict(s),
        "label_suggested": label,
        "label_reason": reason,
        "suspect_scoring": suspect,
        "answer": out.text,
        "assumptions": out.assumptions,
        "metrics_used": out.metrics_used,
        "sql_used": sql_used,
        "all_results": {
            rid: {"columns": r.columns, "rows": r.rows[:RESULT_ROWS_KEPT]}
            for rid, r in all_results.items()
        },
        "cited_results": {
            rid: {"columns": r.columns, "rows": r.rows[:RESULT_ROWS_KEPT]}
            for rid, r in cited.items()
        },
        "trace_id": trace.trace_id,
        "steps": trace.steps,
        "tool_calls": len(trace.tool_calls),
        "tool_errors": sum(1 for t in trace.tool_calls if t.is_error),
        "tokens": tokens,
        "cost_usd": trace.cost_usd,
        "latency_ms": trace.latency_ms,
        "models_served": trace.models_served,
    }
    return record, trace.to_dict()


def run_eval(
    exp: Experiment,
    qset: QuestionSet,
    *,
    base: Settings,
    llm_factory: Callable[[Settings], LLMClient],
    split: str = "all",
    repeat: int = 3,
    workers: int = 4,
    only: list[str] | None = None,
    runs_dir: Path = DEFAULT_RUNS_DIR,
    labels_dir: Path = DEFAULT_LABELS_DIR,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Run the experiment; returns the run directory."""
    from scrollback.evals.report import render_report, summarize

    settings = experiment_settings(base, exp, qset)
    questions = [q for q in qset.by_split(split) if not only or q.id in only]
    if not questions:
        raise ValueError("no questions selected")
    warehouses = build_anchors({q.today for q in questions}, base=settings, params=qset.params)
    golds = compute_golds(qset.model_copy(update={"questions": questions}), warehouses)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + exp.name
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "config.json").write_text(json.dumps({
        "run_id": run_id, "experiment": exp.model_dump(), "split": split, "repeat": repeat,
        "questions": [q.id for q in questions], "question_set_version": qset.version,
        "params": qset.params.model_dump(), "config": config_snapshot(settings),
        "system_prompt_sha256": prompt_sha256(load_system_prompt(settings)),
        "git_commit": _git_commit(), "started_at": datetime.now(UTC).isoformat(),
    }, indent=1, default=str))  # fmt: skip

    llm = llm_factory(settings)
    lock = threading.Lock()
    records: list[dict[str, Any]] = []
    tasks = [(rep, q) for rep in range(1, repeat + 1) for q in questions]
    done = 0

    def work(rep: int, q: Question) -> None:
        nonlocal done
        run_settings = settings.model_copy(deep=True)
        run_settings.paths = run_settings.paths.model_copy(
            update={"charts_dir": run_dir / "charts", "traces_dir": run_dir}
        )
        record, trace = run_one(
            q, golds[q.id], rep, settings=run_settings, llm=llm, db_path=warehouses[q.today]
        )
        with lock:
            records.append(record)
            with (run_dir / "results.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
            if trace is not None:
                with (run_dir / "traces.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(trace, default=str, ensure_ascii=False) + "\n")
            done += 1
            if progress:
                mark = "pass" if record["passed"] else f"FAIL ({record.get('label_suggested')})"
                progress(f"[{done}/{len(tasks)}] r{rep} {q.id} {mark}")

    if workers <= 1:
        for rep, q in tasks:
            work(rep, q)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for future in [pool.submit(work, rep, q) for rep, q in tasks]:
                future.result()

    records.sort(key=lambda r: (r["repeat"], r["question"]))
    summary = summarize(records, qset)
    summary["run_id"] = run_id
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    (run_dir / "report.md").write_text(render_report(summary, records, qset))
    failures = [
        {
            "question": r["question"], "repeat": r["repeat"], "category": r["category"],
            "split": r["split"], "suggested": r.get("label_suggested"),
            "reason": r.get("label_reason"), "suspect_scoring": r.get("suspect_scoring"),
            "label": None,
        }
        for r in records if not r["passed"]
    ]  # fmt: skip
    write_label_file(run_id, failures, labels_dir / f"{run_id}.yaml")
    return run_dir


def load_records(run_dir: Path) -> list[dict[str, Any]]:
    return load_records_file(run_dir / "results.jsonl")


def load_records_file(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return sorted((json.loads(x) for x in lines if x.strip()),
                  key=lambda r: (r["repeat"], r["question"]))  # fmt: skip


def _scores_dict(s: Any) -> dict[str, Any]:
    return {
        "execution": s.execution, "execution_strict": s.execution_strict, "answer": s.answer,
        "gold_stated": s.gold_stated, "metric_selection": s.metric_selection,
        "refusal": s.refusal, "caveat": s.caveat, "caveat_strong": s.caveat_strong,
        "assumptions": s.assumptions,
        "reading": s.reading, "wrong_value": s.wrong_value, "refused": s.refused,
        "fabricated": s.fabricated, "notes": s.notes,
    }  # fmt: skip


def rescore_run(
    run_dir: Path, qset: QuestionSet, *, base: Settings, labels_dir: Path = DEFAULT_LABELS_DIR
) -> dict[str, Any]:
    """Re-score a finished run from its saved answers and results (no API calls).

    Uses the current question set and scorer. The first re-score keeps the original
    results as ``results.original.jsonl``. Confirmed labels carry over to failures that
    remain; failures that now pass are listed under ``rescored_to_pass`` in the label file.
    """
    from scrollback.evals.labels import load_confirmed
    from scrollback.evals.report import render_report, summarize

    records = load_records(run_dir)
    traces = {}
    traces_path = run_dir / "traces.jsonl"
    if traces_path.exists():
        for line in traces_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                t = json.loads(line)
                traces[t["trace_id"]] = t
    qids = sorted({r["question"] for r in records})
    selected = [q for q in qset.questions if q.id in qids]
    missing = sorted(set(qids) - {q.id for q in selected})
    if missing:
        raise ValueError(f"questions no longer in the set: {missing}")
    warehouses = build_anchors({q.today for q in selected}, base=base, params=qset.params)
    golds = compute_golds(qset.model_copy(update={"questions": selected}), warehouses)

    label_path = labels_dir / f"{run_dir.name}.yaml"
    confirmed = load_confirmed(label_path)
    previous_doc = yaml.safe_load(label_path.read_text()) if label_path.exists() else {}
    # Changes are always reported against the run as originally scored.
    original = run_dir / "results.original.jsonl"
    originally = {
        (r["question"], r["repeat"]): r
        for r in (load_records_file(original) if original.exists() else records)
    }
    changed: list[dict[str, Any]] = []
    new_records = []
    for r in records:
        if r["status"] == "harness_error":
            new_records.append(r)
            continue
        q = qset.get(r["question"])
        out = AgentOutput(
            status=r["status"],
            text=r.get("answer", ""),
            result_ids=list(r.get("cited_results", {})),
            metrics_used=r.get("metrics_used", []),
            assumptions=r.get("assumptions", []),
            results={
                rid: ResultTable(c["columns"], c["rows"])
                for rid, c in r.get("cited_results", {}).items()
            },
            all_results=_all_results(r, traces.get(r.get("trace_id"), {})),
            tool_values=describe_table_values(
                traces.get(r.get("trace_id"), {}).get("tool_calls", [])
            ),
        )
        s = score(q, golds[q.id], out)
        tool_calls = traces.get(r.get("trace_id"), {}).get("tool_calls", [])
        label, reason, suspect = None, None, None
        if not s.passed:
            label, reason = suggest_label(
                q, s, status=r["status"], tool_calls=tool_calls, sql_used=r.get("sql_used", [])
            )
            suspect = suspect_scoring(q, s)
        first = originally.get((q.id, r["repeat"]), r)
        if s.passed != first["passed"]:
            changed.append({"question": q.id, "repeat": r["repeat"],
                            "before": first["passed"], "after": s.passed,
                            "old_label": first.get("label_suggested")})  # fmt: skip
        new_records.append({
            **r, "category": q.category, "split": q.split, "passed": s.passed,
            "scores": _scores_dict(s), "label_suggested": label, "label_reason": reason,
            "suspect_scoring": suspect,
        })  # fmt: skip

    if not original.exists():
        (run_dir / "results.jsonl").rename(original)
    (run_dir / "results.jsonl").write_text(
        "".join(json.dumps(r, default=str, ensure_ascii=False) + "\n" for r in new_records)
    )
    summary = summarize(new_records, qset)
    summary["run_id"] = run_dir.name
    summary["rescored_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str))

    failures = []
    for r in new_records:
        if r["passed"]:
            continue
        prev = confirmed.get((r["question"], r["repeat"]), {})
        failures.append({
            "question": r["question"], "repeat": r["repeat"], "category": r["category"],
            "split": r["split"], "suggested": r.get("label_suggested"),
            "reason": r.get("label_reason"), "suspect_scoring": r.get("suspect_scoring"),
            "label": prev.get("label"), **({"note": prev["note"]} if prev.get("note") else {}),
        })  # fmt: skip
    write_label_file(run_dir.name, failures, label_path)
    doc = yaml.safe_load(label_path.read_text())
    for key, value in (previous_doc or {}).items():
        if key not in doc and key not in ("rescored_to_pass", "rescored_to_fail"):
            doc[key] = value  # keep e.g. the owner's confirmation note
    doc["rescored_to_pass"] = [c for c in changed if c["after"]]
    doc["rescored_to_fail"] = [c for c in changed if not c["after"]]
    label_path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100))
    (run_dir / "report.md").write_text(
        render_report(summary, new_records, qset, load_confirmed(label_path))
    )
    return {"changed": changed, "summary": summary}


def _all_results(record: dict[str, Any], trace: dict[str, Any]) -> dict[str, ResultTable]:
    """Every successful run_sql result of a run: from the record, else rebuilt from its trace."""
    if record.get("all_results"):
        return {
            rid: ResultTable(c["columns"], c["rows"]) for rid, c in record["all_results"].items()
        }
    out = {
        rid: ResultTable(c["columns"], c["rows"])
        for rid, c in record.get("cited_results", {}).items()
    }
    for call in trace.get("tool_calls", []):
        if call.get("name") != "run_sql" or call.get("is_error"):
            continue
        try:
            payload = json.loads(call.get("output", ""))
        except json.JSONDecodeError:
            continue  # output was truncated in the trace
        rid = payload.get("result_id")
        if rid and rid not in out:
            out[rid] = ResultTable(payload.get("columns", []), payload.get("rows", []))
    return out
