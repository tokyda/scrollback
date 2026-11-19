# Scrollback

An LLM data agent over your own TikTok watch history. Ask a question in plain English and get an
answer back with the SQL it ran, a chart, and the metric definitions it used.

Status: early build. See [docs/DESIGN.md](docs/DESIGN.md) for the spec and
[docs/ROADMAP.md](docs/ROADMAP.md) for progress.

## Quickstart (synthetic data)

```bash
uv sync
uv run scrollback synth
uv run scrollback ingest data/sample/user_data_tiktok.json
```

Real exports go in `data/raw/`, which is git-ignored. Only synthetic data is committed.
