"""Scrollback command-line interface (DESIGN §8)."""

from __future__ import annotations

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
        str, typer.Option(help="Export format variant: v1_activity or v2_your_activity.")
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


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Question in plain English.")],
    show_sql: Annotated[bool, typer.Option("--show-sql", help="Print the SQL used.")] = False,
    chart: Annotated[bool, typer.Option("--chart", help="Render a chart if useful.")] = False,
) -> None:
    """Ask the agent a question about your viewing history."""
    _not_implemented("ask", "Phase 3")


@app.command()
def enrich(
    limit: Annotated[int, typer.Option(help="Maximum number of videos to enrich.")] = 500,
) -> None:
    """Opt-in oEmbed enrichment of video titles and authors."""
    _not_implemented("enrich", "stretch")


@eval_app.command("run")
def eval_run(
    config: Annotated[str, typer.Option(help="Experiment config YAML.")],
    repeat: Annotated[int, typer.Option(help="Number of repeated runs.")] = 3,
) -> None:
    """Run the eval set against an experiment config."""
    _not_implemented("eval run", "Phase 4")


@eval_app.command("report")
def eval_report(
    run_a: Annotated[str, typer.Argument(help="Baseline run directory.")],
    run_b: Annotated[str | None, typer.Argument(help="Comparison run directory.")] = None,
) -> None:
    """Produce a markdown report for one run or a comparison of two."""
    _not_implemented("eval report", "Phase 4")


@eval_app.command("import-feedback")
def eval_import_feedback() -> None:
    """Turn bad bot feedback into candidate eval questions."""
    _not_implemented("eval import-feedback", "Phase 6")


@kb_app.command("index")
def kb_index() -> None:
    """Embed metrics, docs, schema and examples into DuckDB."""
    _not_implemented("kb index", "Phase 5")


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
def serve_bot() -> None:
    """Run the Telegram bot."""
    _not_implemented("serve bot", "Phase 6")


if __name__ == "__main__":  # pragma: no cover
    app()
