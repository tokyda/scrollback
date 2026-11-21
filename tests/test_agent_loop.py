"""Agent loop with a scripted mock LLM (no network): DESIGN §6.1-6.6, ROADMAP Phase 3."""

import json
from datetime import date
from typing import Any

import duckdb
import pytest
from pydantic import SecretStr

from scrollback.agent.llm import LLMError, LLMResponse, ScriptedLLMClient, Usage, mock_turn
from scrollback.agent.loop import NUDGE, run_agent
from scrollback.config import Settings

TODAY = date(2026, 11, 20)  # "yesterday" = 2026-11-19, the last synthetic day
YESTERDAY_SQL = (
    "SELECT sum(est_watch_sec) / 60.0 AS minutes FROM fct_views "
    "WHERE local_date = DATE '2026-11-19'"
)


def final(text: str, result_ids: list[str], chart_id: str = "") -> tuple[str, dict[str, Any]]:
    return (
        "final_answer",
        {
            "text": text,
            "result_ids": result_ids,
            "metrics_used": ["est_watch_minutes"],
            "assumptions": ["yesterday = 2026-11-19, Asia/Singapore"],
            "chart_id": chart_id,
        },
    )


def run(settings: Settings, turns: list[Any], **kwargs: Any) -> Any:
    llm = ScriptedLLMClient(turns)
    result = run_agent(
        "how many minutes did I watch yesterday",
        settings=settings,
        llm=llm,
        today=TODAY,
        **kwargs,
    )
    return result, llm


def tool_results(llm: ScriptedLLMClient, request_index: int) -> list[dict[str, Any]]:
    """The tool_result blocks sent to the model in request ``request_index``."""
    last = llm.requests[request_index]["messages"][-1]
    assert last["role"] == "user" and isinstance(last["content"], list)
    return last["content"]


def expected_minutes(settings: Settings) -> float:
    con = duckdb.connect(str(settings.paths.warehouse), read_only=True)
    row = con.execute(YESTERDAY_SQL).fetchone()
    con.close()
    assert row is not None
    return float(row[0])


# --------------------------------------------------------------------------- happy path


def test_happy_path(agent_settings: Settings) -> None:
    result, llm = run(
        agent_settings,
        [
            mock_turn(("describe_table", {"table": "fct_views"}), text="Checking the table."),
            mock_turn(("run_sql", {"sql": YESTERDAY_SQL})),
            mock_turn(final("You watched ~X minutes yesterday.", ["r1"])),
        ],
    )
    assert result.status == "answered"
    answer = result.answer
    assert answer is not None
    assert answer.result_ids == ["r1"]
    # sql_used is the SQL that actually ran: the guard's rewrite, with LIMIT added.
    assert len(answer.sql_used) == 1
    assert answer.sql_used[0] == result.trace.tool_calls[1].meta["executed"]
    assert answer.sql_used[0].endswith("LIMIT 200") and "fct_views" in answer.sql_used[0]
    assert answer.assumptions and answer.metrics_used == ["est_watch_minutes"]

    # The model saw the real result of its query.
    sql_result = json.loads(tool_results(llm, 2)[0]["content"])
    assert sql_result["result_id"] == "r1"
    assert sql_result["rows"][0][0] == pytest.approx(expected_minutes(agent_settings), abs=1e-6)

    # Request shape: stable system prompt with the table list, today in the user message.
    first = llm.requests[0]
    assert "fct_views" in first["system"] and "2026-11-20" not in first["system"]
    assert (
        "Today is 2026-11-20 (Friday), timezone Asia/Singapore." in first["messages"][0]["content"]
    )
    assert {t["name"] for t in first["tools"]} == {
        "describe_table", "run_sql", "make_chart", "final_answer",
    }  # fmt: skip
    assert all(t["strict"] for t in first["tools"])


def test_e0_prompt_has_no_metric_or_glossary_content(agent_settings: Settings) -> None:
    _, llm = run(agent_settings, [mock_turn(final("n/a", []))])
    system = llm.requests[0]["system"].lower()
    for forbidden in (
        "search_knowledge", "query_metric", "est_watch_minutes", "estimate", "view_cap",
        "median", "session gap", "binge", "late night", "cannot answer",
    ):  # fmt: skip
        assert forbidden not in system, forbidden


def test_describe_table_is_aggregate_safe_in_e0(agent_settings: Settings) -> None:
    _, llm = run(
        agent_settings,
        [
            mock_turn(
                ("describe_table", {"table": "fct_views"}),
                ("describe_table", {"table": "agg_daily"}),
            ),
            mock_turn(final("n/a", [])),
        ],
    )
    fct, agg = (json.loads(r["content"]) for r in tool_results(llm, 1))
    assert "sample_rows" not in fct and "column_stats" in fct  # no individual records
    assert "description" not in fct and all("description" not in c for c in fct["columns"])
    assert len(agg["sample_rows"]) == 3


def test_descriptions_switch_for_e1(agent_settings: Settings) -> None:
    agent_settings.agent = agent_settings.agent.model_copy(
        update={"describe_include_descriptions": True}
    )
    _, llm = run(
        agent_settings,
        [mock_turn(("describe_table", {"table": "fct_views"})), mock_turn(final("n/a", []))],
    )
    payload = json.loads(tool_results(llm, 1)[0]["content"])
    assert "ESTIMATE" in payload["description"]
    est = next(c for c in payload["columns"] if c["name"] == "est_watch_sec")
    assert "view_cap_sec" in est["description"]


# --------------------------------------------------------------------------- SQL errors


def test_sql_error_then_retry(agent_settings: Settings) -> None:
    result, llm = run(
        agent_settings,
        [
            mock_turn(("run_sql", {"sql": "SELECT sum(watch_seconds) FROM fct_views"})),
            mock_turn(("run_sql", {"sql": YESTERDAY_SQL})),
            mock_turn(final("ok", ["r1"])),
        ],
    )
    assert result.status == "answered"
    err = tool_results(llm, 1)[0]
    assert err["is_error"] is True
    assert err["content"].startswith("[execution_error]") and "watch_seconds" in err["content"]
    ok = json.loads(tool_results(llm, 2)[0]["content"])
    assert ok["result_id"] == "r1"
    codes = [t.error_code for t in result.trace.tool_calls]
    assert codes == ["execution_error", None, None]


def test_sql_retries_exhausted_disables_run_sql(agent_settings: Settings) -> None:
    bad = ("run_sql", {"sql": "SELECT nope FROM fct_views GROUP BY 1"})
    result, llm = run(
        agent_settings,
        [
            mock_turn(bad),  # failure 1
            mock_turn(bad),  # retry 1
            mock_turn(bad),  # retry 2 -> exhausted
            mock_turn(("run_sql", {"sql": YESTERDAY_SQL})),  # disabled, not executed
            mock_turn(final("I could not compute this.", [])),
        ],
    )
    assert result.status == "answered"
    assert "No retries left (2 used)" in tool_results(llm, 3)[0]["content"]
    disabled = tool_results(llm, 4)[0]
    assert disabled["content"].startswith("[sql_disabled]")
    assert [t.error_code for t in result.trace.tool_calls][:4] == [
        "execution_error", "execution_error", "execution_error", "sql_disabled",
    ]  # fmt: skip


def test_success_resets_retry_count(agent_settings: Settings) -> None:
    bad = ("run_sql", {"sql": "SELECT nope FROM fct_views GROUP BY 1"})
    good = ("run_sql", {"sql": YESTERDAY_SQL})
    result, _ = run(
        agent_settings,
        [mock_turn(bad), mock_turn(bad), mock_turn(good), mock_turn(bad), mock_turn(bad),
         mock_turn(good), mock_turn(final("ok", ["r2"]))],
    )  # fmt: skip
    assert result.status == "answered"
    assert "sql_disabled" not in [t.error_code for t in result.trace.tool_calls]


@pytest.mark.parametrize(
    ("sql", "rule"),
    [
        ("SELECT count(*) FROM raw_watch", "table_not_allowed"),
        ("SELECT video_id, event_ts_local FROM fct_views", "not_aggregate"),
        ("DROP TABLE fct_views", "write_or_ddl"),
        ("SELECT getenv('ANTHROPIC_API_KEY')", "function_not_allowed"),
    ],
)
def test_guard_rejection_returned_to_model(agent_settings: Settings, sql: str, rule: str) -> None:
    result, llm = run(
        agent_settings,
        [mock_turn(("run_sql", {"sql": sql})), mock_turn(final("n/a", []))],
    )
    blocked = tool_results(llm, 1)[0]
    assert blocked["is_error"] and blocked["content"].startswith(f"[{rule}]")
    assert result.trace.tool_calls[0].error_code == rule


def test_statement_timeout(agent_settings: Settings) -> None:
    agent_settings.agent = agent_settings.agent.model_copy(update={"statement_timeout_sec": 0.2})
    slow = "SELECT count(*) FROM fct_views a, fct_views b, fct_views c WHERE a.dow + b.dow > c.dow"
    _, llm = run(
        agent_settings, [mock_turn(("run_sql", {"sql": slow})), mock_turn(final("n/a", []))]
    )
    assert tool_results(llm, 1)[0]["content"].startswith("[timeout]")


# --------------------------------------------------------------------------- ending the loop


def test_step_limit(agent_settings: Settings) -> None:
    agent_settings.agent = agent_settings.agent.model_copy(update={"max_steps": 3})
    loop_turn = mock_turn(("describe_table", {"table": "agg_daily"}))
    result, llm = run(agent_settings, [loop_turn] * 5)
    assert result.status == "step_limit"
    assert result.answer is None
    assert len(llm.requests) == 3 and result.trace.steps == 3


def test_text_without_tool_gets_one_nudge(agent_settings: Settings) -> None:
    result, llm = run(
        agent_settings,
        [
            mock_turn(text="You watched about 30 minutes."),
            mock_turn(("run_sql", {"sql": YESTERDAY_SQL})),
            mock_turn(final("ok", ["r1"])),
        ],
    )
    assert result.status == "answered"
    assert llm.requests[1]["messages"][-1] == {"role": "user", "content": NUDGE}


def test_text_twice_ends_without_answer(agent_settings: Settings) -> None:
    result, _ = run(agent_settings, [mock_turn(text="a"), mock_turn(text="b")])
    assert result.status == "no_final_answer" and result.answer is None


def test_refusal(agent_settings: Settings) -> None:
    result, _ = run(agent_settings, [mock_turn(stop_reason="refusal")])
    assert result.status == "refused" and result.answer is None


def test_max_tokens(agent_settings: Settings) -> None:
    result, _ = run(agent_settings, [mock_turn(text="partial", stop_reason="max_tokens")])
    assert result.status == "max_tokens"


def test_llm_error(agent_settings: Settings) -> None:
    def boom(_: list[dict[str, Any]]) -> LLMResponse:
        raise LLMError("rate_limit", "rate limited")

    result, _ = run(agent_settings, [boom])
    assert result.status == "error" and "rate_limit" in (result.trace.error or "")


# --------------------------------------------------------------------------- tool handling


def test_parallel_calls_answered_in_one_message(agent_settings: Settings) -> None:
    result, llm = run(
        agent_settings,
        [
            mock_turn(
                ("run_sql", {"sql": YESTERDAY_SQL}),
                ("run_sql", {"sql": "SELECT count(*) AS views FROM fct_views"}),
            ),
            mock_turn(final("ok", ["r1", "r2"])),
        ],
    )
    blocks = tool_results(llm, 1)  # one user message carrying both results
    assistant = llm.requests[1]["messages"][-2]["content"]
    called = [b["id"] for b in assistant if b["type"] == "tool_use"]
    assert [b["tool_use_id"] for b in blocks] == called and len(called) == 2
    assert result.answer is not None and len(result.answer.sql_used) == 2


def test_final_answer_in_same_turn_as_query(agent_settings: Settings) -> None:
    result, _ = run(
        agent_settings,
        [mock_turn(final("ok", ["r1"]), ("run_sql", {"sql": YESTERDAY_SQL}))],
    )
    assert result.status == "answered" and result.answer is not None
    assert result.answer.result_ids == ["r1"]


def test_unknown_result_id_is_corrected(agent_settings: Settings) -> None:
    result, llm = run(
        agent_settings,
        [
            mock_turn(("run_sql", {"sql": YESTERDAY_SQL})),
            mock_turn(final("ok", ["r9"])),
            mock_turn(final("ok", ["r1"])),
        ],
    )
    assert tool_results(llm, 2)[0]["content"].startswith("[unknown_result_id]")
    assert result.status == "answered"


def test_invalid_tool_input(agent_settings: Settings) -> None:
    _, llm = run(
        agent_settings,
        [
            mock_turn(("run_sql", {"query": "SELECT 1"}), ("teleport", {})),
            mock_turn(final("n/a", [])),
        ],
    )
    invalid, unknown = tool_results(llm, 1)
    assert invalid["content"].startswith("[invalid_input]")
    assert unknown["content"].startswith("[unknown_tool]")


def test_chart(agent_settings: Settings) -> None:
    daily = (
        "SELECT local_date, est_watch_min FROM agg_daily "
        "WHERE local_date BETWEEN '2026-11-13' AND '2026-11-19' ORDER BY 1"
    )
    result, llm = run(
        agent_settings,
        [
            mock_turn(("run_sql", {"sql": daily})),
            mock_turn(("make_chart", {"result_id": "r1", "kind": "bar", "x": "local_date",
                                      "y": "est_watch_min", "title": "Last 7 days"})),
            mock_turn(final("ok", ["r1"], chart_id="c1")),
        ],
        want_chart=True,
    )  # fmt: skip
    assert result.answer is not None and result.answer.chart_path
    chart = result.answer.chart_path
    assert chart.endswith("_c1.png")
    with open(chart, "rb") as fh:
        assert fh.read(8) == b"\x89PNG\r\n\x1a\n"
    assert "Include a chart" in llm.requests[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"result_id": "r7", "kind": "bar", "x": "a", "y": "b", "title": "t"},
         "unknown_result_id"),
        ({"result_id": "r1", "kind": "bar", "x": "local_date", "y": "nope", "title": "t"},
         "unknown_column"),
        ({"result_id": "r1", "kind": "pie", "x": "local_date", "y": "views", "title": "t"},
         "invalid_input"),
    ],
)  # fmt: skip
def test_chart_errors(agent_settings: Settings, args: dict[str, Any], code: str) -> None:
    _, llm = run(
        agent_settings,
        [
            mock_turn(("run_sql", {"sql": "SELECT local_date, views FROM agg_daily LIMIT 3"})),
            mock_turn(("make_chart", args)),
            mock_turn(final("n/a", [])),
        ],
    )
    assert tool_results(llm, 2)[0]["content"].startswith(f"[{code}]")


# --------------------------------------------------------------------------- tracing


def test_trace_written_with_tokens_and_cost(agent_settings: Settings) -> None:
    usage = Usage(input_tokens=1500, output_tokens=200, cache_read_input_tokens=3000,
                  cache_creation_input_tokens=400)  # fmt: skip
    turns = [
        mock_turn(("run_sql", {"sql": YESTERDAY_SQL}), usage=usage, model="claude-sonnet-5-5"),
        mock_turn(final("ok", ["r1"]), usage=usage, model="claude-sonnet-5-5"),
    ]
    result, _ = run(agent_settings, turns)
    assert result.trace_path is not None and result.trace_db_error is None

    line = json.loads(result.trace_path.read_text().splitlines()[-1])
    assert line["trace_id"] == result.trace.trace_id
    assert line["status"] == "answered" and line["steps"] == 2
    assert line["tokens"] == {"input": 3000, "output": 400, "cache_read": 6000, "cache_write": 800}
    expected_cost = 2 * (1500 * 2.00 + 200 * 10.00 + 3000 * 0.10 + 400 * 2.50) / 1e6
    assert line["cost_usd"] == pytest.approx(expected_cost)
    assert line["prompt_version"] == "1" and line["model"] == "mock-model"
    assert [t["name"] for t in line["tool_calls"]] == ["run_sql", "final_answer"]
    assert line["tool_calls"][0]["meta"]["executed"].endswith("LIMIT 200")
    assert line["final_answer"]["sql_used"]
    assert line["config"]["llm"]["effort"] == "medium"

    con = duckdb.connect(str(agent_settings.paths.warehouse), read_only=True)
    row = con.execute(
        "SELECT status, steps, n_tool_calls, input_tokens, cost_usd FROM agent_traces "
        "WHERE trace_id = ?",
        [result.trace.trace_id],
    ).fetchone()
    con.close()
    assert row is not None
    assert row[:4] == ("answered", 2, 2, 3000) and row[4] == pytest.approx(expected_cost)


def test_trace_records_prompt_version_and_hash(agent_settings: Settings) -> None:
    import hashlib

    from scrollback.agent.loop import load_system_prompt

    result, _ = run(agent_settings, [mock_turn(final("n/a", []))])
    expected = hashlib.sha256(load_system_prompt(agent_settings).encode()).hexdigest()
    assert result.trace.system_prompt_sha256 == expected
    line = json.loads(result.trace_path.read_text().splitlines()[-1])
    assert line["system_prompt_sha256"] == expected and line["prompt_version"] == "1"
    con = duckdb.connect(str(agent_settings.paths.warehouse), read_only=True)
    row = con.execute(
        "SELECT prompt_version, system_prompt_sha256 FROM agent_traces WHERE trace_id = ?",
        [result.trace.trace_id],
    ).fetchone()
    con.close()
    assert row == ("1", expected)


def test_agent_traces_from_before_phase_5_are_migrated(tmp_path: Any) -> None:
    from scrollback.tracing import Trace, write_db

    db = tmp_path / "old.duckdb"
    con = duckdb.connect(str(db))
    old_ddl = (
        "CREATE TABLE agent_traces (trace_id VARCHAR PRIMARY KEY, started_at TIMESTAMP NOT NULL, "
        "question VARCHAR NOT NULL, today DATE NOT NULL, local_tz VARCHAR NOT NULL, "
        "model VARCHAR NOT NULL, models_served VARCHAR NOT NULL, "
        "prompt_version INTEGER NOT NULL, status VARCHAR NOT NULL, steps INTEGER NOT NULL, "
        "n_tool_calls INTEGER NOT NULL, n_tool_errors INTEGER NOT NULL, "
        "input_tokens BIGINT NOT NULL, output_tokens BIGINT NOT NULL, "
        "cache_read_tokens BIGINT NOT NULL, cache_write_tokens BIGINT NOT NULL, "
        "cost_usd DOUBLE, latency_ms DOUBLE NOT NULL, final_answer JSON, llm_calls JSON NOT NULL, "
        "tool_calls JSON NOT NULL, config JSON NOT NULL, error VARCHAR)"
    )
    con.execute(old_ddl)
    con.execute(
        "INSERT INTO agent_traces VALUES ('old', '2026-11-19 00:00:00', 'q', '2026-11-19', "
        "'UTC', 'm', 'm', 1, 'answered', 1, 0, 0, 0, 0, 0, 0, NULL, 1.0, NULL, '[]', '[]', "
        "'{}', NULL)"
    )
    con.close()
    trace = Trace(trace_id="new", started_at="2026-11-21T00:00:00", question="q",
                  today="2026-11-21", local_tz="UTC", model="m", prompt_version="1_search",
                  config={}, system_prompt_sha256="abc")  # fmt: skip
    write_db(trace, db)
    con = duckdb.connect(str(db), read_only=True)
    rows = con.execute(
        "SELECT trace_id, prompt_version, system_prompt_sha256 FROM agent_traces ORDER BY 1"
    ).fetchall()
    con.close()
    assert rows == [("new", "1_search", "abc"), ("old", "1", None)]


def test_prompt_version_is_text() -> None:
    from scrollback.config import AgentConfig

    assert AgentConfig.model_validate({"prompt_version": 1}).prompt_version == "1"
    assert AgentConfig.model_validate({"prompt_version": "1_search"}).prompt_version == "1_search"
    with pytest.raises(ValueError):
        AgentConfig.model_validate({"prompt_version": "../secrets"})


def test_failed_runs_are_traced_too(agent_settings: Settings) -> None:
    agent_settings.agent = agent_settings.agent.model_copy(update={"max_steps": 1})
    result, _ = run(agent_settings, [mock_turn(("describe_table", {"table": "agg_daily"}))])
    line = json.loads(result.trace_path.read_text().splitlines()[-1]) if result.trace_path else {}
    assert line["status"] == "step_limit" and line["final_answer"] is None


def test_trace_never_contains_the_api_key(agent_settings: Settings) -> None:
    agent_settings.secrets.anthropic_api_key = SecretStr("sk-ant-api03-SUPERSECRETVALUE")
    result, _ = run(
        agent_settings,
        [mock_turn(("run_sql", {"sql": YESTERDAY_SQL})), mock_turn(final("ok", ["r1"]))],
    )
    assert result.trace_path is not None
    assert "SUPERSECRETVALUE" not in result.trace_path.read_text()
    con = duckdb.connect(str(agent_settings.paths.warehouse), read_only=True)
    dump = str(con.execute("SELECT * FROM agent_traces").fetchall())
    con.close()
    assert "SUPERSECRETVALUE" not in dump


def test_agent_traces_table_is_not_queryable_by_agent(agent_settings: Settings) -> None:
    run(agent_settings, [mock_turn(final("n/a", []))])  # creates agent_traces
    _, llm = run(
        agent_settings,
        [mock_turn(("run_sql", {"sql": "SELECT count(*) FROM agent_traces"})),
         mock_turn(final("n/a", []))],
    )  # fmt: skip
    assert tool_results(llm, 1)[0]["content"].startswith("[table_not_allowed]")


def test_e_minus_1_staging_scope(agent_settings: Settings) -> None:
    agent_settings.agent = agent_settings.agent.model_copy(update={"tables": "staging"})
    sql = "SELECT count(*) AS views FROM stg_watch WHERE local_date = DATE '2026-11-19'"
    result, llm = run(
        agent_settings,
        [
            mock_turn(("describe_table", {"table": "stg_watch"}),
                      ("run_sql", {"sql": "SELECT count(*) FROM fct_views"})),
            mock_turn(("run_sql", {"sql": sql})),
            mock_turn(final("167 views", ["r1"])),
        ],
    )  # fmt: skip
    first = llm.requests[0]
    assert "- stg_watch" in first["system"] and "fct_views" not in first["system"]
    enum = first["tools"][0]["input_schema"]["properties"]["table"]["enum"]
    assert enum == ["stg_watch", "stg_like", "stg_favorite", "stg_search", "stg_share"]
    described, blocked = tool_results(llm, 1)
    assert "column_stats" in json.loads(described["content"])  # staging is row-level
    assert blocked["content"].startswith("[table_not_allowed]")
    assert json.loads(tool_results(llm, 2)[0]["content"])["rows"] == [[167]]
    assert result.status == "answered"


# --------------------------------------------------------------------------- search_knowledge


def search_settings(settings: Settings) -> Settings:
    settings.agent.prompt_version = "1_search"
    settings.agent.search_knowledge = True
    return settings


def test_prompt_and_tool_must_agree(agent_settings: Settings) -> None:
    from scrollback.agent.loop import PromptToolMismatchError, load_system_prompt

    agent_settings.agent.search_knowledge = True  # prompt v1 doesn't mention the tool
    with pytest.raises(PromptToolMismatchError, match="does not mention"):
        load_system_prompt(agent_settings)
    agent_settings.agent.search_knowledge = False
    agent_settings.agent.prompt_version = "1_search"  # mentions it, tool off
    with pytest.raises(PromptToolMismatchError, match="mentions"):
        load_system_prompt(agent_settings)


def test_prompt_v1_search_is_v1_plus_one_tool_line() -> None:
    from scrollback.agent.loop import PROMPTS_DIR

    v1 = (PROMPTS_DIR / "system_v1.md").read_text().splitlines()
    v1s = (PROMPTS_DIR / "system_v1_search.md").read_text().splitlines()
    added = [line for line in v1s if line not in v1]
    assert len(v1s) == len(v1) + 1 and len(added) == 1
    assert added[0].startswith("- search_knowledge(query):")


def test_prompt_v2_is_v1_search_plus_one_rule() -> None:
    from scrollback.agent.loop import PROMPTS_DIR

    v1s = (PROMPTS_DIR / "system_v1_search.md").read_text()
    v2 = (PROMPTS_DIR / "system_v2.md").read_text()
    rule = (
        "- Before computing any metric, look up its definition with search_knowledge and "
        "compute it as\n  defined.\n"
    )
    assert v2.replace(rule, "", 1) == v1s


def test_search_knowledge_tool_in_the_loop(agent_settings: Settings) -> None:
    settings = search_settings(agent_settings)
    turns = [
        mock_turn(("search_knowledge", {"query": "screen time watch minutes"})),
        mock_turn(("run_sql", {"sql": YESTERDAY_SQL})),
        mock_turn(final("About 103 minutes.", ["r1"])),
    ]
    result, llm = run(settings, turns)
    assert result.status == "answered"
    names = [t["name"] for t in llm.requests[0]["tools"]]
    assert names[0] == "search_knowledge" and "run_sql" in names
    call = result.trace.tool_calls[0]
    assert call.name == "search_knowledge" and not call.is_error
    assert call.meta["retrieved"][0] == "metric:est_watch_minutes"
    payload = json.loads(tool_results(llm, 1)[0]["content"])
    assert payload["results"][0]["id"] == "metric:est_watch_minutes"
    assert "{view_cap_sec}" not in json.dumps(payload)  # params filled in


def test_search_knowledge_is_absent_by_default(agent_settings: Settings) -> None:
    turns = [
        mock_turn(("search_knowledge", {"query": "watch time"})),
        mock_turn(final("n/a", [])),
    ]
    result, llm = run(agent_settings, turns)
    assert "search_knowledge" not in [t["name"] for t in llm.requests[0]["tools"]]
    assert result.trace.tool_calls[0].error_code == "unknown_tool"
