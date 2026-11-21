"""Agent run traces (DESIGN §6.6): one JSON line per run and one row in ``agent_traces``.

Traces are the raw material for bad-case analysis. They hold the question, model, prompt
version, a config snapshot, every LLM call (tokens, cost, latency, stop reason) and every tool
call (input, duration, error code, a capped copy of the output), plus the final answer.

Privacy: traces contain aggregated query results and live only in git-ignored places
(``traces/`` and the warehouse). They never contain secrets: the config snapshot is built from
non-secret settings only.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

TRACE_OUTPUT_CHARS = 4000

AGENT_TRACES_DDL = """
CREATE TABLE IF NOT EXISTS agent_traces (
    trace_id VARCHAR PRIMARY KEY,
    started_at TIMESTAMP NOT NULL,
    question VARCHAR NOT NULL,
    today DATE NOT NULL,
    local_tz VARCHAR NOT NULL,
    model VARCHAR NOT NULL,
    models_served VARCHAR NOT NULL,
    prompt_version VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    steps INTEGER NOT NULL,
    n_tool_calls INTEGER NOT NULL,
    n_tool_errors INTEGER NOT NULL,
    input_tokens BIGINT NOT NULL,
    output_tokens BIGINT NOT NULL,
    cache_read_tokens BIGINT NOT NULL,
    cache_write_tokens BIGINT NOT NULL,
    cost_usd DOUBLE,
    latency_ms DOUBLE NOT NULL,
    final_answer JSON,
    llm_calls JSON NOT NULL,
    tool_calls JSON NOT NULL,
    config JSON NOT NULL,
    error VARCHAR,
    system_prompt_sha256 VARCHAR
)
"""
AGENT_TRACES_COLUMNS = (
    "trace_id, started_at, question, today, local_tz, model, models_served, prompt_version, "
    "status, steps, n_tool_calls, n_tool_errors, input_tokens, output_tokens, "
    "cache_read_tokens, cache_write_tokens, cost_usd, latency_ms, final_answer, llm_calls, "
    "tool_calls, config, error, system_prompt_sha256"
)


def migrate_agent_traces(con: duckdb.DuckDBPyConnection) -> None:
    """Bring an agent_traces table from before Phase 5 up to date: prompt_version becomes text
    ("1", "1_search") and system_prompt_sha256 is added. Existing rows keep their values."""
    types = dict(
        con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'agent_traces'"
        ).fetchall()
    )
    if types.get("prompt_version") not in (None, "VARCHAR"):
        con.execute("ALTER TABLE agent_traces ALTER prompt_version TYPE VARCHAR")
    if "system_prompt_sha256" not in types:
        con.execute("ALTER TABLE agent_traces ADD COLUMN system_prompt_sha256 VARCHAR")


def new_trace_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]


@dataclass
class LLMCallTrace:
    step: int
    model_served: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float | None
    latency_ms: float
    text: str
    tool_calls: list[str]
    request_id: str | None = None


@dataclass
class ToolCallTrace:
    step: int
    name: str
    input: dict[str, Any]
    duration_ms: float
    is_error: bool
    error_code: str | None
    output: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trace:
    trace_id: str
    started_at: str
    question: str
    today: str
    local_tz: str
    model: str
    prompt_version: str
    config: dict[str, Any]
    status: str = "running"
    final_answer: dict[str, Any] | None = None
    llm_calls: list[LLMCallTrace] = field(default_factory=list)
    tool_calls: list[ToolCallTrace] = field(default_factory=list)
    latency_ms: float = 0.0
    error: str | None = None
    system_prompt_sha256: str = ""

    @property
    def steps(self) -> int:
        return len(self.llm_calls)

    def tokens(self) -> dict[str, int]:
        return {
            "input": sum(c.input_tokens for c in self.llm_calls),
            "output": sum(c.output_tokens for c in self.llm_calls),
            "cache_read": sum(c.cache_read_tokens for c in self.llm_calls),
            "cache_write": sum(c.cache_write_tokens for c in self.llm_calls),
        }

    @property
    def cost_usd(self) -> float | None:
        costs = [c.cost_usd for c in self.llm_calls]
        if any(c is None for c in costs):
            return None
        return float(sum(c for c in costs if c is not None))

    @property
    def models_served(self) -> list[str]:
        return sorted({c.model_served for c in self.llm_calls})

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(
            steps=self.steps,
            tokens=self.tokens(),
            cost_usd=self.cost_usd,
            models_served=self.models_served,
        )
        return data


def cap(text: str, limit: int = TRACE_OUTPUT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


def write_jsonl(trace: Trace, traces_dir: Path) -> Path:
    traces_dir.mkdir(parents=True, exist_ok=True)
    path = traces_dir / f"{trace.started_at[:10]}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(trace.to_dict(), default=str, ensure_ascii=False) + "\n")
    return path


def write_db(trace: Trace, db_path: Path) -> None:
    con = duckdb.connect(str(db_path))
    try:
        con.execute(AGENT_TRACES_DDL)
        migrate_agent_traces(con)
        tokens = trace.tokens()
        con.execute(
            f"INSERT INTO agent_traces ({AGENT_TRACES_COLUMNS}) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                trace.trace_id,
                trace.started_at,
                trace.question,
                trace.today,
                trace.local_tz,
                trace.model,
                ",".join(trace.models_served),
                trace.prompt_version,
                trace.status,
                trace.steps,
                len(trace.tool_calls),
                sum(1 for t in trace.tool_calls if t.is_error),
                tokens["input"],
                tokens["output"],
                tokens["cache_read"],
                tokens["cache_write"],
                trace.cost_usd,
                trace.latency_ms,
                json.dumps(trace.final_answer, default=str) if trace.final_answer else None,
                json.dumps([asdict(c) for c in trace.llm_calls], default=str),
                json.dumps([asdict(t) for t in trace.tool_calls], default=str),
                json.dumps(trace.config, default=str),
                trace.error,
                trace.system_prompt_sha256,
            ],
        )
    finally:
        con.close()


def write_trace(trace: Trace, traces_dir: Path, db_path: Path | None) -> tuple[Path, str | None]:
    """Write the JSONL line, then the DuckDB row. Returns (jsonl path, db error or None)."""
    path = write_jsonl(trace, traces_dir)
    if db_path is None:
        return path, None
    try:
        write_db(trace, db_path)
    except duckdb.Error as exc:  # e.g. the warehouse is locked by another process
        return path, f"agent_traces not written: {type(exc).__name__}"
    return path, None
