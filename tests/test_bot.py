"""The Telegram bot against a fake Bot API and the scripted LLM (no network, synthetic data)."""

import json
import logging
from datetime import date
from typing import Any

import pytest

from scrollback.agent.llm import LLMError, ScriptedLLMClient, mock_turn
from scrollback.bot.telegram_api import TelegramAPI
from scrollback.bot.telegram_bot import (
    TODAY_QUESTION,
    WEEK_QUESTION,
    ScrollbackBot,
    pack,
    wants_chart,
)
from scrollback.config import Secrets, Settings
from scrollback.feedback import load_feedback
from tests.telegram_fake import FAKE_TOKEN, FakeTelegram

OWNER, STRANGER = 4242, 777
SQL = "SELECT sum(est_watch_sec) / 60.0 AS minutes FROM fct_views WHERE local_date = '2026-11-19'"
SERIES = (
    "SELECT local_date, sum(est_watch_sec) / 60.0 AS minutes FROM fct_views "
    "WHERE local_date BETWEEN '2026-11-13' AND '2026-11-19' GROUP BY 1 ORDER BY 1"
)


def _answer(text: str = "You watched about 42 minutes on 19 Nov.", chart_id: str = "") -> Any:
    return (
        "final_answer",
        {
            "text": text,
            "result_ids": ["r1"],
            "metrics_used": ["est_watch_minutes"],
            "assumptions": ["most recent day in the data = 2026-11-19 (Asia/Singapore)"],
            "chart_id": chart_id,
        },
    )


SIMPLE = [mock_turn(("run_sql", {"sql": SQL})), mock_turn(_answer())]
WITH_CHART = [
    mock_turn(("run_sql", {"sql": SERIES})),
    mock_turn(
        ("make_chart", {"result_id": "r1", "kind": "bar", "x": "local_date", "y": "minutes",
                        "title": "Last 7 days"})
    ),
    mock_turn(_answer("13-19 Nov: 290 min in total.", chart_id="c1")),
]  # fmt: skip


@pytest.fixture
def bot_settings(agent_settings: Settings) -> Settings:
    s = agent_settings.model_copy(deep=True)
    s.secrets = Secrets(telegram_bot_token=FAKE_TOKEN, telegram_allowed_chat_ids=[OWNER])
    return s


@pytest.fixture
def fake() -> FakeTelegram:
    return FakeTelegram()


def _bot(settings: Settings, fake: FakeTelegram, turns: list[Any]) -> ScrollbackBot:
    api = TelegramAPI(FAKE_TOKEN, transport=fake.transport)
    llm = ScriptedLLMClient(turns)
    return ScrollbackBot(api, settings, llm, today=lambda: date(2026, 11, 22), sleep=lambda s: None)


def _llm(bot: ScrollbackBot) -> ScriptedLLMClient:
    assert isinstance(bot.llm, ScriptedLLMClient)
    return bot.llm


def _first_user_message(bot: ScrollbackBot) -> str:
    return str(_llm(bot).requests[0]["messages"][0]["content"])


# --------------------------------------------------------------------------- allowlist


def test_strangers_are_ignored_silently(bot_settings: Settings, fake: FakeTelegram) -> None:
    bot = _bot(bot_settings, fake, [])
    bot.handle_update(fake.message(STRANGER, "how much did I watch?"))
    bot.handle_update(fake.message(STRANGER, "/today"))
    bot.handle_update(fake.callback(STRANGER, "sql:20261122T000000-aaaaaaaa"))
    assert fake.calls == [] and _llm(bot).requests == [] and bot.ignored == 3


def test_bot_requires_allowlist_and_aggregate_only(bot_settings: Settings) -> None:
    api = TelegramAPI(FAKE_TOKEN, transport=FakeTelegram().transport)
    empty = bot_settings.model_copy(deep=True)
    empty.secrets = Secrets(telegram_bot_token=FAKE_TOKEN)
    with pytest.raises(ValueError, match="empty"):
        ScrollbackBot(api, empty, ScriptedLLMClient([]))
    full = bot_settings.model_copy(deep=True)
    full.agent.result_mode = "full"
    with pytest.raises(ValueError, match="aggregate_only"):
        ScrollbackBot(api, full, ScriptedLLMClient([]))


# --------------------------------------------------------------------------- questions


def test_free_text_answer_with_show_sql_button(bot_settings: Settings, fake: FakeTelegram) -> None:
    bot = _bot(bot_settings, fake, SIMPLE)
    bot.handle_update(fake.message(OWNER, "how many minutes did I watch on 19 Nov?"))
    assert fake.sent("sendChatAction")
    (reply,) = fake.sent("sendMessage")
    text = reply.params["text"]
    assert text.startswith("You watched about 42 minutes on 19 Nov.")
    assert "most recent day in the data = 2026-11-19" in text
    assert "<b>Metrics:</b> est_watch_minutes" in text
    button = reply.params["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"] == "Show SQL" and button["callback_data"].startswith("sql:")
    assert len(button["callback_data"].encode()) <= 64
    assert not fake.sent("sendPhoto")
    msg = _first_user_message(bot)
    assert "Today is 2026-11-22" in msg and "Include a chart" not in msg
    traces = list(bot_settings.paths.traces_dir.glob("*.jsonl"))
    assert len(traces) == 1


def test_chart_keyword_requests_chart_and_sends_photo(
    bot_settings: Settings, fake: FakeTelegram
) -> None:
    bot = _bot(bot_settings, fake, WITH_CHART)
    bot.handle_update(fake.message(OWNER, "plot my watch time for the week of 13 Nov"))
    assert "Include a chart" in _first_user_message(bot)
    (photo,) = fake.sent("sendPhoto")
    assert photo.files["photo"].startswith(b"\x89PNG")
    reply_id = photo.params["reply_parameters"]
    assert '"message_id"' in reply_id  # photo replies to the answer message


def test_show_sql_callback_sends_executed_sql(bot_settings: Settings, fake: FakeTelegram) -> None:
    bot = _bot(bot_settings, fake, SIMPLE)
    bot.handle_update(fake.message(OWNER, "minutes on 19 Nov?"))
    data = fake.sent("sendMessage")[0].params["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]
    bot.handle_update(fake.callback(OWNER, data))
    assert fake.sent("answerCallbackQuery")
    sql_msg = fake.sent("sendMessage")[-1].params["text"]
    assert sql_msg.startswith('<pre><code class="language-sql">')
    assert "sum(est_watch_sec)" in sql_msg.lower() and "LIMIT 200" in sql_msg


def test_show_sql_unknown_trace(bot_settings: Settings, fake: FakeTelegram) -> None:
    bot = _bot(bot_settings, fake, [])
    bot.handle_update(fake.callback(OWNER, "sql:20261122T000000-deadbeef"))
    assert fake.sent("sendMessage")[-1].params["text"] == "No SQL found for that answer."


def test_today_and_week_are_fixed_questions(bot_settings: Settings, fake: FakeTelegram) -> None:
    bot = _bot(bot_settings, fake, SIMPLE)
    bot.handle_update(fake.message(OWNER, "/today"))
    msg = _first_user_message(bot)
    assert f"Question: {TODAY_QUESTION}" in msg and "Include a chart" not in msg
    assert "most recent day in my data" in TODAY_QUESTION and "date you used" in TODAY_QUESTION

    bot = _bot(bot_settings, fake, WITH_CHART)
    bot.handle_update(fake.message(OWNER, "/week@ScrollbackBot"))
    msg = _first_user_message(bot)
    assert f"Question: {WEEK_QUESTION}" in msg and "Include a chart" in msg
    assert "last 7 days of data" in WEEK_QUESTION and "dates you used" in WEEK_QUESTION
    assert fake.sent("sendPhoto")


def test_metrics_command_needs_no_llm(bot_settings: Settings, fake: FakeTelegram) -> None:
    bot = _bot(bot_settings, fake, [])
    bot.handle_update(fake.message(OWNER, "/metrics"))
    texts = [c.params["text"] for c in fake.sent("sendMessage")]
    joined = "\n".join(texts)
    assert "<code>est_watch_minutes</code>" in joined and "{" not in joined
    assert all(len(t) <= 4096 for t in texts)
    assert _llm(bot).requests == []


def test_help_unknown_and_non_text(bot_settings: Settings, fake: FakeTelegram) -> None:
    bot = _bot(bot_settings, fake, [])
    bot.handle_update(fake.message(OWNER, "/start"))
    bot.handle_update(fake.message(OWNER, "/nope"))
    bot.handle_update(fake.message(OWNER, "/wrapped"))
    update = fake.message(OWNER, "")
    bot.handle_update(update)
    texts = [c.params["text"] for c in fake.sent("sendMessage")]
    assert "/feedback good|bad" in texts[0]
    assert texts[1].startswith("Unknown command.")
    assert "stretch" in texts[2]
    assert texts[3] == "Send me a question as text."


def test_agent_failure_statuses_and_exceptions(bot_settings: Settings, fake: FakeTelegram) -> None:
    def boom(messages: Any) -> Any:
        raise LLMError("rate_limit", "rate limited")

    bot = _bot(bot_settings, fake, [boom])
    bot.handle_update(fake.message(OWNER, "q"))
    reply = fake.sent("sendMessage")[-1].params
    assert reply["text"].startswith("No answer: the model call failed")
    assert "reply_markup" not in reply and "<code>" in reply["text"]  # trace footer

    bot = _bot(bot_settings, fake, [])
    bot.db_path = bot_settings.paths.warehouse.parent / "missing.duckdb"
    bot.safe_handle(fake.message(OWNER, "q"))
    assert "Nothing was answered" in fake.sent("sendMessage")[-1].params["text"]


# --------------------------------------------------------------------------- feedback


def test_feedback_on_last_answer_and_on_reply(bot_settings: Settings, fake: FakeTelegram) -> None:
    bot = _bot(bot_settings, fake, SIMPLE + WITH_CHART)
    bot.handle_update(fake.message(OWNER, "first question"))
    first_answer_id = fake.next_message_id  # the fake's last id is the answer just sent
    bot.handle_update(fake.message(OWNER, "second, as a chart"))
    photo_id = fake.next_message_id

    bot.handle_update(fake.message(OWNER, "/feedback bad wrong day"))
    bot.handle_update(fake.message(OWNER, "/feedback good", reply_to=first_answer_id))
    bot.handle_update(fake.message(OWNER, "/feedback BAD chart axis", reply_to=photo_id))
    fbs = load_feedback(bot_settings.paths.traces_dir)
    assert [(f.rating, f.note) for f in fbs] == [
        ("bad", "wrong day"), ("good", ""), ("bad", "chart axis")
    ]  # fmt: skip
    first, second = (json.loads(line)["trace_id"] for p in
                     sorted(bot_settings.paths.traces_dir.glob("*.jsonl"))
                     for line in p.read_text().splitlines())  # fmt: skip
    assert [f.trace_id for f in fbs] == [second, first, second]
    confirm = fake.sent("sendMessage")[-1].params["text"]
    assert "recorded <b>bad</b>" in confirm and "candidate eval question" in confirm


def test_feedback_usage_and_nothing_to_rate(bot_settings: Settings, fake: FakeTelegram) -> None:
    bot = _bot(bot_settings, fake, [])
    bot.handle_update(fake.message(OWNER, "/feedback meh"))
    bot.handle_update(fake.message(OWNER, "/feedback bad"))
    bot.handle_update(fake.message(OWNER, "/feedback bad", reply_to=12345))
    texts = [c.params["text"] for c in fake.sent("sendMessage")]
    assert texts[0].startswith("Usage:")
    assert texts[1].startswith("Nothing to rate yet")
    assert texts[2].startswith("That message isn't one of my answers")
    assert load_feedback(bot_settings.paths.traces_dir) == []


# --------------------------------------------------------------------------- polling


def test_run_drops_pending_then_answers(bot_settings: Settings, fake: FakeTelegram) -> None:
    old = fake.message(OWNER, "sent while the bot was off")
    fake.queue(old)
    fake.after_drop.append([fake.message(OWNER, "now")])
    bot = _bot(bot_settings, fake, SIMPLE)
    bot.run(max_polls=1)
    polls = fake.sent("getUpdates")
    assert polls[0].params["offset"] == -1
    assert polls[1].params["offset"] == old["update_id"] + 1
    assert len(fake.sent("sendMessage")) == 1  # only "now" was answered
    assert len(_llm(bot).requests) == 2 and "Question: now" in _first_user_message(bot)


def test_run_backs_off_on_errors(bot_settings: Settings, fake: FakeTelegram) -> None:
    fake.failures["getUpdates"] = (502, "Bad Gateway")
    slept: list[float] = []
    bot = _bot(bot_settings, fake, [])
    bot._sleep = slept.append
    bot.settings.bot.drop_pending_on_start = False
    bot.run(max_polls=3)
    assert slept == [1.0, 2.0, 4.0]


def test_token_never_in_logs_or_traces(
    bot_settings: Settings, fake: FakeTelegram, caplog: pytest.LogCaptureFixture
) -> None:
    fake.failures["sendMessage"] = (400, f"Bad Request: token {FAKE_TOKEN}")
    fake.failures["getUpdates"] = (401, f"Unauthorized {FAKE_TOKEN}")
    bot = _bot(bot_settings, fake, SIMPLE)
    bot.settings.bot.drop_pending_on_start = False
    with caplog.at_level(logging.DEBUG):
        bot.safe_handle(fake.message(OWNER, "q"))
        bot.run(max_polls=1)
    assert "reply failed" in caplog.text and "getUpdates failed" in caplog.text
    assert FAKE_TOKEN not in caplog.text
    assert "q" not in [r.getMessage() for r in caplog.records]  # no question text logged
    for path in bot_settings.paths.traces_dir.rglob("*.jsonl"):
        assert FAKE_TOKEN not in path.read_text()


# --------------------------------------------------------------------------- helpers


def test_wants_chart_words() -> None:
    kw = ["chart", "plot", "graph", "trend"]
    assert wants_chart("Plot my week", kw) and wants_chart("any TREND?", kw)
    assert not wants_chart("how much did I watch", kw)
    assert not wants_chart("paragraphs", kw)  # whole words only


def test_pack_respects_limit() -> None:
    parts = pack(["a" * 3000, "b" * 3000, "c" * 10], limit=4000)
    assert len(parts) == 2 and all(len(p) <= 4000 for p in parts)


def test_long_answer_is_cut_to_fit(bot_settings: Settings, fake: FakeTelegram) -> None:
    bot = _bot(bot_settings, fake, [mock_turn(("run_sql", {"sql": SQL})),
                                    mock_turn(_answer("x" * 6000))])  # fmt: skip
    bot.handle_update(fake.message(OWNER, "q"))
    text = fake.sent("sendMessage")[0].params["text"]
    assert len(text) <= 4096 and "…(cut)" in text and "<code>" in text
