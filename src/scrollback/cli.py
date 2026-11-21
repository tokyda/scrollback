"""Scrollback command-line interface (DESIGN §8)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from scrollback.config import ConfigError, Settings, load_config

app = typer.Typer(
    name="scrollback",
    help="An LLM data agent over your own TikTok watch history.",
    no_args_is_help=True,
    add_completion=False,
)
eval_app = typer.Typer(help="Evaluation harness (DESIGN §7).", no_args_is_help=True)
kb_app = typer.Typer(help="Knowledge base for retrieval (DESIGN §5).", no_args_is_help=True)
alerts_app = typer.Typer(help="Anomaly alerts (DESIGN §9).", no_args_is_help=True)
digest_app = typer.Typer(help="Weekly 'Scrollback Wrapped' digest.", no_args_is_help=True)
serve_app = typer.Typer(help="Run the HTTP API or the Telegram bot.", no_args_is_help=True)
inspect_app = typer.Typer(
    help="Inspect warehouse distributions (aggregates only; safe on real data).",
    no_args_is_help=True,
)

app.add_typer(eval_app, name="eval")
app.add_typer(kb_app, name="kb")
app.add_typer(alerts_app, name="alerts")
app.add_typer(digest_app, name="digest")
app.add_typer(serve_app, name="serve")
app.add_typer(inspect_app, name="inspect")

NOT_IMPLEMENTED_EXIT_CODE = 2


def _not_implemented(command: str, when: str) -> None:
    typer.echo(f"`scrollback {command}` is not implemented yet ({when}).", err=True)
    raise typer.Exit(NOT_IMPLEMENTED_EXIT_CODE)


ConfigOption = Annotated[
    Path | None, typer.Option("--config", help="Path to config.yaml (default: ./config.yaml).")
]


DbOption = Annotated[
    Path | None, typer.Option(help="DuckDB file (default: paths.warehouse in config).")
]


def _settings(config: Path | None) -> Settings:
    try:
        return load_config(config)
    except ConfigError as exc:
        typer.echo(f"Config error: {exc}", err=True)
        raise typer.Exit(1) from None


ExperimentOption = Annotated[
    Path | None,
    typer.Option(
        "--experiment",
        help="Experiment file whose llm/agent overrides to use (default: interactive.experiment).",
    ),
]
NoExperimentOption = Annotated[
    bool,
    typer.Option("--no-experiment", help="Use config.yaml's llm/agent as they are."),
]


def _interactive_settings(
    config: Path | None, experiment: Path | None, no_experiment: bool
) -> tuple[Settings, str]:
    """Settings for ask and the bot, and a label naming the config in use."""
    from scrollback.experiments import interactive_settings

    base = _settings(config)
    try:
        settings, exp = interactive_settings(base, experiment, use_experiment=not no_experiment)
    except (OSError, ValueError) as exc:
        typer.echo(f"Config error: experiment could not be loaded: {exc}", err=True)
        raise typer.Exit(1) from None
    return settings, (exp.name if exp else "config.yaml")


def _run_build(settings: Settings, db_path: Path, quiet: bool = False) -> None:
    """Run `dbt build`; exit 1 when there is no data or any model/test fails."""
    from scrollback.warehouse.db import has_view_data
    from scrollback.warehouse.dbt_runner import DbtError, run_dbt

    if not has_view_data(db_path):
        typer.echo(
            f"No watch history in {db_path.name}: run `scrollback ingest <export>` first.",
            err=True,
        )
        raise typer.Exit(1)
    try:
        result = run_dbt(settings, db_path, quiet=quiet)
    except DbtError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(result.summary())
    if not result.success:
        for name in result.failures:
            typer.echo(f"  failed: {name}", err=True)
        raise typer.Exit(1)


@app.command()
def ingest(
    path: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, help="Export file (.json, or .zip with one)."),
    ],
    db: DbOption = None,
    build: Annotated[
        bool, typer.Option("--build/--no-build", help="Run `dbt build` after loading.")
    ] = True,
    config: ConfigOption = None,
) -> None:
    """Ingest a TikTok data export into the warehouse, then run `dbt build`."""
    from scrollback.ingest.detect import UnknownExportFormatError
    from scrollback.ingest.load import ExportReadError, ingest_file
    from scrollback.warehouse.db import connect

    settings = _settings(config)
    db_path = db or settings.paths.warehouse
    con = connect(db_path)
    try:
        summary = ingest_file(con, path)
    except (UnknownExportFormatError, ExportReadError) as exc:
        typer.echo(f"Ingest failed: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        con.close()

    # Counts only: never print record contents (DESIGN §4.6).
    typer.echo(
        f"Ingested {summary.source_file} (variant {summary.variant}, id {summary.ingest_id})"
    )
    header = f"{'dataset':<10}{'read':>8}{'valid':>8}{'new':>8}{'dup':>8}{'rejected':>10}"
    typer.echo(header)
    for name, c in summary.datasets.items():
        if not c.present:
            typer.echo(f"{name:<10}{'(absent)':>8}")
            continue
        typer.echo(f"{name:<10}{c.read:>8}{c.valid:>8}{c.new:>8}{c.duplicate:>8}{c.rejected:>10}")
    typer.echo(
        f"{'total':<10}{summary.rows_read:>8}{'':>8}{summary.rows_new:>8}"
        f"{summary.rows_duplicate:>8}{summary.rows_rejected:>10}"
    )
    typer.echo(f"New rejects stored: {summary.rejects_new}")
    for name, c in summary.datasets.items():
        if c.unknown_fields:
            typer.echo(
                f"Warning: {name} records have unrecognised fields {c.unknown_fields}; "
                "kept in the extras column. Check DESIGN §4.1 and consider mapping them.",
                err=True,
            )
    if summary.ignored_sections:
        typer.echo(f"Ignored sections: {len(summary.ignored_sections)}")
    if not build:
        typer.echo("dbt build: skipped (--no-build)")
        return
    try:
        _run_build(settings, db_path)
    except typer.Exit as exc:
        if exc.exit_code:
            typer.echo("Raw data was loaded; fix the problem above and run `scrollback build`.")
        raise


SAMPLE_FILE_NAME = "user_data_tiktok.json"


@app.command()
def synth(
    days: Annotated[int, typer.Option(min=1, help="Number of days of history.")] = 180,
    seed: Annotated[int, typer.Option(help="Random seed (same seed = same output).")] = 42,
    end: Annotated[
        str, typer.Option(help="Last local date covered, YYYY-MM-DD (fixed for determinism).")
    ] = "2026-11-19",
    variant: Annotated[
        str,
        typer.Option(
            help="Export format variant: v1_activity, v2_your_activity or v3_likes_favorites."
        ),
    ] = "v2_your_activity",
    local_tz: Annotated[
        str, typer.Option(help="Timezone the synthetic person lives in.")
    ] = "Asia/Singapore",
    out: Annotated[
        Path | None,
        typer.Option(help=f"Output file (default: <paths.sample_dir>/{SAMPLE_FILE_NAME})."),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Generate a synthetic TikTok export (and a manifest of expected counts)."""
    from datetime import date

    from scrollback.synth.generator import synthesize, write

    try:
        end_date = date.fromisoformat(end)
        result = synthesize(days=days, seed=seed, end=end_date, variant=variant, local_tz=local_tz)
    except ValueError as exc:
        typer.echo(f"synth failed: {exc}", err=True)
        raise typer.Exit(1) from None
    out_path = out or _settings(config).paths.sample_dir / SAMPLE_FILE_NAME
    manifest_path = write(result, out_path)
    totals = result.manifest["expected"]["totals"]
    typer.echo(
        f"Wrote {out_path} ({variant}, {result.manifest['start']}..{result.manifest['end']}, "
        f"seed {seed})"
    )
    typer.echo(
        f"records {totals['records']}, expected valid_unique {totals['valid_unique']}, "
        f"duplicates {totals['duplicates']}, rejected {totals['rejected']}"
    )
    typer.echo(f"Manifest: {manifest_path}")


@app.command()
def build(
    db: DbOption = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Hide dbt's progress log.")] = False,
    config: ConfigOption = None,
) -> None:
    """Run `dbt build` (models + data tests) with parameters from config.yaml."""
    settings = _settings(config)
    _run_build(settings, db or settings.paths.warehouse, quiet=quiet)


@inspect_app.command("gaps")
def inspect_gaps(db: DbOption = None, config: ConfigOption = None) -> None:
    """Distribution of gaps between consecutive views, to tune session/cap parameters."""
    import duckdb

    from scrollback.warehouse.inspect import WarehouseNotBuiltError, format_gap_report, gap_stats

    settings = _settings(config)
    db_path = db or settings.paths.warehouse
    if not db_path.exists():
        typer.echo(f"No warehouse at {db_path.name}: run `scrollback ingest` first.", err=True)
        raise typer.Exit(1)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        stats = gap_stats(
            con,
            view_cap_sec=settings.metrics.view_cap_sec,
            session_gap_min=settings.metrics.session_gap_min,
        )
    except WarehouseNotBuiltError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    finally:
        con.close()
    typer.echo(format_gap_report(stats))


def _format_answer(result: object, show_sql: bool) -> str:
    """Render an answered run in the DESIGN §6.5 layout."""
    from scrollback.agent.loop import AgentResult

    assert isinstance(result, AgentResult) and result.answer is not None
    a = result.answer
    lines = [a.text, ""]
    if a.metrics_used:
        lines.append(f"Metrics: {', '.join(a.metrics_used)}")
    if a.assumptions:
        lines.append("Assumptions:")
        lines += [f"  - {x}" for x in a.assumptions]
    n = len(a.sql_used)
    if show_sql and n:
        lines.append(f"SQL ({n} {'query' if n == 1 else 'queries'}):")
        for sql in a.sql_used:
            lines.append(f"  {sql}")
    elif n:
        lines.append(f"SQL: {n} {'query' if n == 1 else 'queries'} (use --show-sql to see)")
    else:
        lines.append("SQL: none (the answer is not based on a query result)")
    if a.chart_path:
        lines.append(f"Chart: {a.chart_path}")
    return "\n".join(lines)


def _trace_line(result: object) -> str:
    from scrollback.agent.loop import AgentResult

    assert isinstance(result, AgentResult)
    t = result.trace
    tokens = sum(t.tokens().values())
    cost = f"${t.cost_usd:.4f}" if t.cost_usd is not None else "cost n/a"
    return (
        f"Trace {t.trace_id} · {t.status} · {t.steps} steps · {len(t.tool_calls)} tool calls · "
        f"{tokens:,} tokens · {cost} · {t.latency_ms / 1000:.1f}s"
    )


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Question in plain English.")],
    show_sql: Annotated[bool, typer.Option("--show-sql", help="Print the SQL used.")] = False,
    chart: Annotated[bool, typer.Option("--chart", help="Ask for a supporting chart.")] = False,
    today: Annotated[
        str | None,
        typer.Option(help="Pretend today is YYYY-MM-DD (default: today in local_tz)."),
    ] = None,
    db: DbOption = None,
    config: ConfigOption = None,
    experiment: ExperimentOption = None,
    no_experiment: NoExperimentOption = False,
) -> None:
    """Ask the agent a question about your viewing history.

    Runs the same config as the Telegram bot: config.yaml plus the llm/agent overrides of
    interactive.experiment (the best eval config), on config.yaml's calibrated metric params."""
    from datetime import date

    from scrollback.agent import llm as llm_module
    from scrollback.agent.llm import LLMError
    from scrollback.agent.loop import STATUS_MESSAGES, run_agent
    from scrollback.warehouse.db import has_view_data

    settings, config_label = _interactive_settings(config, experiment, no_experiment)
    db_path = db or settings.paths.warehouse
    if not has_view_data(db_path):
        typer.echo("No data: run `scrollback ingest <export>` first.", err=True)
        raise typer.Exit(1)
    try:
        today_date = date.fromisoformat(today) if today else None
    except ValueError:
        typer.echo(f"--today must be YYYY-MM-DD, got {today!r}", err=True)
        raise typer.Exit(2) from None
    try:
        client = llm_module.create_llm_client(settings)
    except LLMError as exc:
        typer.echo(f"LLM setup failed: {exc}", err=True)
        raise typer.Exit(1) from None

    result = run_agent(
        question, settings=settings, llm=client, db_path=db_path, today=today_date,
        want_chart=chart,
    )  # fmt: skip
    if result.status == "answered":
        typer.echo(_format_answer(result, show_sql))
    else:
        message = STATUS_MESSAGES.get(result.status, f"No answer ({result.status}).")
        if result.status == "error" and result.trace.error:
            message += f": {result.trace.error}"
        typer.echo(message, err=True)
    typer.echo("")
    typer.echo(f"{_trace_line(result)} · {config_label}")
    if result.trace_db_error:
        typer.echo(f"Warning: {result.trace_db_error}", err=True)
    if result.status != "answered":
        raise typer.Exit(1)


@app.command()
def enrich(
    limit: Annotated[int, typer.Option(help="Maximum number of videos to enrich.")] = 500,
) -> None:
    """Opt-in oEmbed enrichment of video titles and authors."""
    _not_implemented("enrich", "stretch")


@eval_app.command("check-gold")
def eval_check_gold(
    update: Annotated[
        bool, typer.Option("--update", help="Rewrite eval/gold_snapshot.json from current golds.")
    ] = False,
    show: Annotated[bool, typer.Option("--show", help="Print every gold answer.")] = False,
    config: ConfigOption = None,
) -> None:
    """Validate eval/questions.yaml and every gold SQL (runs in CI)."""
    from pydantic import ValidationError

    from scrollback.evals.gold import check_gold, compute_golds, snapshot_drift, write_snapshot
    from scrollback.evals.questions import load_questions
    from scrollback.evals.warehouses import build_anchors

    try:
        qset = load_questions()
    except ValidationError as exc:
        typer.echo(f"questions.yaml is invalid:\n{exc}", err=True)
        raise typer.Exit(1) from None
    warehouses = build_anchors(qset.anchors, base=_settings(config), params=qset.params)
    try:
        golds = compute_golds(qset, warehouses)
    except Exception as exc:  # a gold SQL failed to run
        typer.echo(f"gold SQL failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(1) from None
    problems = check_gold(qset, golds)
    if show:
        for q in qset.questions:
            g = golds[q.id]
            value = q.answer_type if g.gold is None else g.gold.rows
            typer.echo(f"{q.id} [{q.category}/{q.split}] {value}")
            for i, alt in enumerate(g.alternatives, 1):
                typer.echo(f"      alt{i}: {alt.rows}")
    if update and not problems:
        write_snapshot(golds)
        typer.echo("gold snapshot updated")
    else:
        problems += snapshot_drift(golds)
    n_dev = len(qset.by_split("dev"))
    typer.echo(
        f"{len(qset.questions)} questions ({n_dev} dev / {len(qset.questions) - n_dev} test), "
        f"{len(qset.anchors)} anchors"
    )
    if problems:
        for problem in problems:
            typer.echo(f"  PROBLEM {problem}", err=True)
        raise typer.Exit(1)
    typer.echo("gold check: ok")


@eval_app.command("run")
def eval_run(
    config: Annotated[Path, typer.Option(exists=True, help="Experiment YAML.")],
    split: Annotated[str, typer.Option(help="dev | test | all")] = "all",
    repeat: Annotated[int, typer.Option(min=1, help="Repeats of the whole set.")] = 3,
    workers: Annotated[int, typer.Option(min=1, help="Parallel agent runs.")] = 4,
    only: Annotated[str | None, typer.Option(help="Comma-separated question ids.")] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Actually run (costs money). Without it: estimate only.")
    ] = False,
    app_config: Annotated[
        Path | None, typer.Option("--app-config", help="Path to config.yaml.")
    ] = None,
) -> None:
    """Run an experiment over the eval set; without --yes, only print the cost estimate."""
    from scrollback.agent import llm as llm_module
    from scrollback.evals.questions import load_questions
    from scrollback.evals.run import (
        DEFAULT_RUNS_DIR,
        estimate_cost,
        experiment_settings,
        load_experiment,
        run_eval,
    )

    if split not in ("dev", "test", "all"):
        typer.echo("--split must be dev, test or all", err=True)
        raise typer.Exit(2)
    base = _settings(app_config)
    exp = load_experiment(config)
    qset = load_questions()
    ids = [x.strip() for x in only.split(",")] if only else None
    questions = [q for q in qset.by_split(split) if not ids or q.id in ids]
    settings = experiment_settings(base, exp, qset)
    history = [*base.paths.traces_dir.glob("*.jsonl"), *DEFAULT_RUNS_DIR.glob("*/traces.jsonl")]
    est = estimate_cost(len(questions) * repeat, settings.llm.model, history)
    typer.echo(
        f"Experiment {exp.name}: {len(questions)} questions ({split}) x {repeat} repeats = "
        f"{est.runs} agent runs on {settings.llm.model} (effort {settings.llm.effort})"
    )
    typer.echo(
        f"Estimated cost: ${est.total_usd:.2f} (about ${est.per_run_usd:.4f} per run; "
        f"up to ~${est.high_usd:.2f} if runs are p90-expensive). Basis: {est.basis}."
    )
    if not yes:
        typer.echo("Not running. Re-run with --yes to start (this calls the API and costs money).")
        return
    run_dir = run_eval(
        exp, qset, base=base, llm_factory=llm_module.create_llm_client, split=split,
        repeat=repeat, workers=workers, only=ids, progress=typer.echo,
    )  # fmt: skip
    typer.echo(f"Run written to {run_dir}")
    typer.echo((run_dir / "report.md").read_text().split("## Sub-scores")[0])


@eval_app.command("report")
def eval_report(
    run_a: Annotated[Path, typer.Argument(exists=True, file_okay=False, help="Run directory.")],
    run_b: Annotated[
        Path | None, typer.Argument(exists=True, file_okay=False, help="Run to compare with.")
    ] = None,
    split: Annotated[
        str, typer.Option(help="Compare only this split: dev | test | all (two runs only).")
    ] = "all",
) -> None:
    """Re-render a run's report (using confirmed labels if any), or compare two runs."""

    from scrollback.evals.labels import load_confirmed
    from scrollback.evals.questions import load_questions
    from scrollback.evals.report import compare, render_report, summarize
    from scrollback.evals.run import DEFAULT_LABELS_DIR, load_records

    qset = load_questions()
    if split not in ("dev", "test", "all"):
        typer.echo("--split must be dev, test or all", err=True)
        raise typer.Exit(2)
    if run_b is not None:
        # Summarise from the records so both runs cover the same split and current scores.
        summaries = []
        for run in (run_a, run_b):
            recs = [r for r in load_records(run) if split == "all" or r["split"] == split]
            summ = summarize(recs, qset)
            summ["run_id"] = run.name
            summaries.append(summ)
        typer.echo(compare(summaries[0], summaries[1], split=split))
        return
    records = load_records(run_a)
    summary = summarize(records, qset)
    summary["run_id"] = run_a.name
    confirmed = load_confirmed(DEFAULT_LABELS_DIR / f"{run_a.name}.yaml")
    report = render_report(summary, records, qset, confirmed)
    (run_a / "report.md").write_text(report)
    typer.echo(report)


@eval_app.command("rescore")
def eval_rescore(
    run: Annotated[Path, typer.Argument(exists=True, file_okay=False, help="Run directory.")],
    config: ConfigOption = None,
) -> None:
    """Re-score a finished run with the current questions and scorer (no API calls)."""
    from scrollback.evals.questions import load_questions
    from scrollback.evals.run import DEFAULT_LABELS_DIR, rescore_run

    result = rescore_run(
        run, load_questions(), base=_settings(config), labels_dir=DEFAULT_LABELS_DIR
    )
    for c in result["changed"]:
        arrow = "pass" if c["after"] else "FAIL"
        typer.echo(f"  {c['question']} r{c['repeat']}: -> {arrow} (was {c['old_label']})")
    acc = result["summary"]["accuracy"]

    def pct(key: str) -> str:
        mean = acc[key]["mean"]
        return "n/a" if mean is None else f"{mean:.1%}"

    typer.echo(
        f"{len(result['changed'])} runs changed (against the original scoring). "
        f"Overall {pct('overall')}, dev {pct('dev')}, test {pct('test')}"
    )


@eval_app.command("retrieval")
def eval_retrieval(
    k: Annotated[int | None, typer.Option(min=1, help="Top k (default: llm.retrieval_k).")] = None,
    split: Annotated[str, typer.Option(help="dev | test | all")] = "dev",
    experiment: Annotated[
        Path | None, typer.Option(exists=True, help="Use this experiment's kb_sources.")
    ] = None,
    show_misses: Annotated[bool, typer.Option("--misses", help="List missed chunks.")] = False,
    config: ConfigOption = None,
) -> None:
    """Offline recall@k: search each question's own text and look for its expected_knowledge.
    No API calls. Dev by default: the test split is held out."""
    from scrollback.evals.questions import load_questions
    from scrollback.evals.retrieval import retrieval_report
    from scrollback.evals.run import experiment_settings, load_experiment

    if split not in ("dev", "test", "all"):
        typer.echo("--split must be dev, test or all", err=True)
        raise typer.Exit(2)
    qset = load_questions()
    settings = _settings(config)
    if experiment is not None:
        settings = experiment_settings(settings, load_experiment(experiment), qset)
    typer.echo(retrieval_report(qset, settings, split=split, k=k, show_misses=show_misses))


@eval_app.command("import-feedback")
def eval_import_feedback(
    out: Annotated[
        Path | None,
        typer.Option(help="Candidates file (default: eval/candidates/feedback.yaml, git-ignored)."),
    ] = None,
    traces_dir: Annotated[
        Path | None, typer.Option(help="Traces folder (default: paths.traces_dir).")
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Turn bad bot feedback into candidate eval questions for review.

    Writes to a git-ignored review file, never to eval/questions.yaml. Prints counts only."""
    from scrollback.evals.feedback_import import DEFAULT_CANDIDATES_PATH, import_feedback
    from scrollback.evals.questions import load_questions

    settings = _settings(config)
    try:
        summary = import_feedback(
            traces_dir or settings.paths.traces_dir,
            out or DEFAULT_CANDIDATES_PATH,
            load_questions(),
        )
    except ValueError as exc:
        typer.echo(f"Cannot import: {exc}", err=True)
        raise typer.Exit(1) from None
    typer.echo(
        f"{summary.feedback} feedback lines; {summary.bad} answers rated bad (latest rating). "
        f"Added {summary.added} candidates, {summary.already_in_file} already in the file."
    )
    if summary.missing_traces:
        typer.echo(f"{summary.missing_traces} bad ratings have no trace yet; not added.")
    if summary.matches_eval_set:
        typer.echo(f"{summary.matches_eval_set} new candidates ask a question already in the set.")
    if summary.now_good:
        typer.echo(f"{summary.now_good} candidates in the file are now rated good; review them.")
    typer.echo(
        f"Review: {summary.path} (git-ignored; copy accepted ones to questions.yaml by hand)"
    )


@kb_app.command("index")
def kb_index(
    rebuild: Annotated[bool, typer.Option("--rebuild", help="Rebuild even if up to date.")] = False,
    config: ConfigOption = None,
) -> None:
    """Embed metrics, docs, table descriptions and examples into the knowledge index."""
    from collections import Counter

    from scrollback.kb.index import ensure_index

    settings = _settings(config)
    index = ensure_index(settings, rebuild=rebuild)
    by_source = Counter(c.source for c in index.chunks)
    counts = ", ".join(f"{k} {v}" for k, v in sorted(by_source.items()))
    typer.echo(
        f"{len(index.chunks)} chunks ({counts}), model {index.model}, "
        f"hash {index.content_hash[:12]}, {settings.paths.kb_index.name}"
    )


@kb_app.command("search")
def kb_search(
    query: Annotated[str, typer.Argument(help="What to look up.")],
    k: Annotated[
        int | None, typer.Option(min=1, help="Chunks to return (default: retrieval_k).")
    ] = None,
    show_text: Annotated[bool, typer.Option("--text", help="Print each chunk's text.")] = False,
    config: ConfigOption = None,
) -> None:
    """Search the knowledge base the way search_knowledge does (agent.kb_sources apply)."""
    from scrollback.kb.retrieve import search

    settings = _settings(config)
    for hit in search(query, settings, k=k):
        typer.echo(f"{hit.score:.4f}  {hit.chunk.id}")
        if show_text:
            typer.echo("    " + hit.text.replace("\n", "\n    "))


@alerts_app.command("check")
def alerts_check() -> None:
    """Check yesterday's metrics for anomalies."""
    _not_implemented("alerts check", "stretch")


@digest_app.command("send")
def digest_send() -> None:
    """Send the weekly digest."""
    _not_implemented("digest send", "stretch")


@serve_app.command("api")
def serve_api() -> None:
    """Run the FastAPI service."""
    _not_implemented("serve api", "stretch")


@serve_app.command("bot")
def serve_bot(
    db: DbOption = None,
    config: ConfigOption = None,
    experiment: ExperimentOption = None,
    no_experiment: NoExperimentOption = False,
) -> None:
    """Run the Telegram bot (long polling; Ctrl+C to stop).

    Answers only chats in TELEGRAM_ALLOWED_CHAT_IDS, with the same config as `ask`
    (interactive.experiment), and only in aggregate_only mode. Each question costs money."""
    import logging
    import signal

    from scrollback.bot.lock import BotAlreadyRunningError, BotLock, git_commit
    from scrollback.bot.telegram_api import RedactingFilter
    from scrollback.warehouse.db import has_view_data

    settings, config_label = _interactive_settings(config, experiment, no_experiment)
    token_secret = settings.secrets.telegram_bot_token
    token = token_secret.get_secret_value() if token_secret else ""
    problems = []
    if not token:
        problems.append("TELEGRAM_BOT_TOKEN is not set (.env)")
    if not settings.secrets.telegram_allowed_chat_ids:
        problems.append("TELEGRAM_ALLOWED_CHAT_IDS is empty (.env); the bot would answer no one")
    if settings.agent.result_mode != "aggregate_only":
        problems.append("agent.result_mode must be aggregate_only for the bot (DESIGN §4.6)")
    db_path = db or settings.paths.warehouse
    if not has_view_data(db_path):
        problems.append(f"no data in {db_path.name}: run `scrollback ingest <export>` first")
    if problems:
        for problem in problems:
            typer.echo(f"Cannot start the bot: {problem}", err=True)
        raise typer.Exit(1)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingFilter(token))
    logging.getLogger("matplotlib").setLevel(logging.WARNING)  # "categorical units" chatter

    # One bot per machine: Telegram lets only one client poll a token (DESIGN §8).
    commit = git_commit(settings.root)
    lock = BotLock(settings.paths.traces_dir, commit=commit)
    try:
        lock.acquire()
    except BotAlreadyRunningError as exc:
        typer.echo(f"Cannot start the bot: {exc}", err=True)
        raise typer.Exit(1) from None
    if lock.replaced is not None:
        typer.echo(f"Removed a stale bot lock (PID {lock.replaced.pid} is no longer running).")
    previous_term = signal.signal(signal.SIGTERM, _interrupt)  # so `kill` releases the lock
    try:
        _run_bot(settings, token, config_label, db_path, commit)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        lock.release()


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def _run_bot(
    settings: Settings,
    token: str,
    config_label: str,
    db_path: Path,
    commit: str | None,
) -> None:
    from scrollback.agent import llm as llm_module
    from scrollback.agent.llm import LLMError
    from scrollback.bot.telegram_api import TelegramAPI, TelegramError
    from scrollback.bot.telegram_bot import ScrollbackBot

    if settings.agent.search_knowledge:
        from scrollback.kb.index import ensure_index

        ensure_index(settings)  # build or load now, not on the first question
    try:
        client = llm_module.create_llm_client(settings)
    except LLMError as exc:
        typer.echo(f"LLM setup failed: {exc}", err=True)
        raise typer.Exit(1) from None
    api = TelegramAPI(token, timeout_sec=settings.bot.poll_timeout_sec + 30)
    bot = ScrollbackBot(api, settings, client, db_path=db_path)
    n = len(settings.secrets.telegram_allowed_chat_ids)
    typer.echo(
        f"Bot running (PID {os.getpid()}, commit {commit or 'unknown'}): {config_label} "
        f"({settings.llm.model}, "
        f"prompt v{settings.agent.prompt_version}, effort {settings.llm.effort}), "
        f"{n} allowed chat{'s' if n != 1 else ''}, "
        f"warehouse {db_path.name}, {settings.agent.result_mode}. Ctrl+C to stop."
    )
    try:
        bot.run()
    except KeyboardInterrupt:
        typer.echo("Bot stopped.")
    except TelegramError as exc:  # already token-free
        typer.echo(f"Bot stopped: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        api.close()


if __name__ == "__main__":  # pragma: no cover
    app()
