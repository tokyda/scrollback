"""The agent loop v0 (DESIGN §6.1): a plain tool-calling loop, no framework.

    system prompt (role, tools, table list, rules)            byte-stable -> cacheable
    user: today + timezone + question (+ chart request)
    -> LLM <-> tools, at most agent.max_steps LLM calls
    -> final_answer(text, result_ids, metrics_used, assumptions, chart_id)

Statuses: answered | step_limit | refused | no_final_answer | max_tokens | error.
Only ``answered`` carries an answer; every other status ends without inventing a number.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from string import Template
from typing import Any
from zoneinfo import ZoneInfo

from scrollback.agent.llm import LLMClient, LLMError
from scrollback.agent.pricing import estimate_cost
from scrollback.agent.tools import (
    MART_TABLES,
    TOOL_SCHEMAS,
    FinalAnswer,
    QueryResult,
    ToolContext,
    execute_tool,
    open_readonly,
)
from scrollback.config import Settings
from scrollback.tracing import LLMCallTrace, ToolCallTrace, Trace, cap, new_trace_id, write_trace

PROMPTS_DIR = Path(__file__).parent / "prompts"
NUDGE = (
    "Please finish by calling the final_answer tool. If you could not compute the answer, "
    "call final_answer anyway and say what is missing."
)
AGGREGATE_RULE = (
    "\n- Query results must be aggregated (COUNT, SUM, AVG, ... or GROUP BY), or come from\n"
    "  agg_* / dim_* tables. Row-level results are rejected."
)


@dataclass
class AgentResult:
    status: str
    answer: FinalAnswer | None
    trace: Trace
    trace_path: Path | None = None
    trace_db_error: str | None = None
    results: dict[str, QueryResult] = field(default_factory=dict)  # every successful run_sql


def load_system_prompt(settings: Settings) -> str:
    version = settings.agent.prompt_version
    path = PROMPTS_DIR / f"system_v{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt version {version} not found: {path.name}")
    rule = AGGREGATE_RULE if settings.agent.result_mode == "aggregate_only" else ""
    return Template(path.read_text(encoding="utf-8")).substitute(
        tables="\n".join(f"- {t}" for t in MART_TABLES), result_mode_rule=rule
    )


def local_today(settings: Settings) -> date:
    return datetime.now(ZoneInfo(settings.timezone.local_tz)).date()


def user_message(question: str, today: date, tz: str, want_chart: bool) -> str:
    lines = [f"Today is {today.isoformat()} ({today.strftime('%A')}), timezone {tz}.", ""]
    lines.append(f"Question: {question.strip()}")
    if want_chart:
        lines += ["", "Include a chart (make_chart) that supports the answer."]
    return "\n".join(lines)


def config_snapshot(settings: Settings) -> dict[str, Any]:
    """Non-secret settings that affect behaviour, recorded with every trace."""
    return {
        "llm": settings.llm.model_dump(),
        "agent": settings.agent.model_dump(),
        "metrics": settings.metrics.model_dump(),
        "timezone": settings.timezone.model_dump(),
    }


def run_agent(
    question: str,
    *,
    settings: Settings,
    llm: LLMClient,
    db_path: Path | None = None,
    today: date | None = None,
    want_chart: bool = False,
    write_traces: bool = True,
) -> AgentResult:
    db_path = db_path or settings.paths.warehouse
    today = today or local_today(settings)
    tz = settings.timezone.local_tz
    trace = Trace(
        trace_id=new_trace_id(),
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        question=question,
        today=today.isoformat(),
        local_tz=tz,
        model=llm.model,
        prompt_version=settings.agent.prompt_version,
        config=config_snapshot(settings),
    )
    started = time.perf_counter()
    system = load_system_prompt(settings)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": user_message(question, today, tz, want_chart)}
    ]
    answer: FinalAnswer | None = None
    status = "step_limit"
    nudged = False

    con = open_readonly(db_path)
    ctx = ToolContext(con=con, settings=settings, trace_id=trace.trace_id)
    try:
        for step in range(1, settings.agent.max_steps + 1):
            try:
                resp = llm.complete(system=system, messages=messages, tools=TOOL_SCHEMAS)
            except LLMError as exc:
                status, trace.error = "error", str(exc)
                break
            trace.llm_calls.append(
                LLMCallTrace(
                    step=step,
                    model_served=resp.model,
                    stop_reason=resp.stop_reason,
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens,
                    cache_read_tokens=resp.usage.cache_read_input_tokens,
                    cache_write_tokens=resp.usage.cache_creation_input_tokens,
                    cost_usd=estimate_cost(resp.model, resp.usage),
                    latency_ms=round(resp.latency_ms, 1),
                    text=cap(resp.text),
                    tool_calls=[c.name for c in resp.tool_calls],
                    request_id=resp.request_id,
                )
            )
            if resp.stop_reason == "refusal":
                status = "refused"
                break
            if resp.stop_reason == "max_tokens":
                status = "max_tokens"
                break
            messages.append({"role": "assistant", "content": resp.assistant_content})

            if not resp.tool_calls:
                if nudged:
                    status = "no_final_answer"
                    break
                nudged = True
                messages.append({"role": "user", "content": NUDGE})
                continue

            # Run every call from this turn; final_answer last so its result_ids can refer to
            # queries made in the same turn. All results go back in one user message.
            calls = sorted(resp.tool_calls, key=lambda c: c.name == "final_answer")
            results: list[dict[str, Any]] = []
            for call in calls:
                t0 = time.perf_counter()
                outcome = execute_tool(ctx, call.name, call.input)
                trace.tool_calls.append(
                    ToolCallTrace(
                        step=step,
                        name=call.name,
                        input=call.input,
                        duration_ms=round((time.perf_counter() - t0) * 1000, 1),
                        is_error=outcome.is_error,
                        error_code=outcome.error_code,
                        output=cap(outcome.content),
                        meta=outcome.meta,
                    )
                )
                if outcome.final is not None and answer is None:
                    answer = outcome.final
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": outcome.content,
                }
                if outcome.is_error:
                    block["is_error"] = True
                results.append(block)
            if answer is not None:
                status = "answered"
                break
            messages.append({"role": "user", "content": results})
    finally:
        con.close()

    trace.status = status
    trace.final_answer = asdict(answer) if answer else None
    trace.latency_ms = round((time.perf_counter() - started) * 1000, 1)
    result = AgentResult(status=status, answer=answer, trace=trace, results=dict(ctx.results))
    if write_traces:
        result.trace_path, result.trace_db_error = write_trace(
            trace, settings.paths.traces_dir, db_path
        )
    return result
