"""Telegram client against the in-memory fake: calls, errors, and the token never leaking."""

import logging
from pathlib import Path

import httpx
import pytest

from scrollback.bot.telegram_api import (
    REDACTED,
    RedactingFilter,
    TelegramAPI,
    TelegramError,
    quiet_http_loggers,
    redact,
)
from tests.telegram_fake import FAKE_TOKEN, FakeTelegram


@pytest.fixture
def fake() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def api(fake: FakeTelegram) -> TelegramAPI:
    return TelegramAPI(FAKE_TOKEN, transport=fake.transport)


def test_send_message_and_reply_markup(fake: FakeTelegram, api: TelegramAPI) -> None:
    kb = {"inline_keyboard": [[{"text": "Show SQL", "callback_data": "sql:t1"}]]}
    mid = api.send_message(42, "<b>hi</b>", reply_markup=kb, reply_to=7)
    assert mid > 0
    (call,) = fake.sent("sendMessage")
    assert call.params["chat_id"] == 42 and call.params["parse_mode"] == "HTML"
    assert call.params["reply_markup"] == kb
    assert call.params["reply_parameters"]["message_id"] == 7


def test_send_message_truncates_to_limit(fake: FakeTelegram, api: TelegramAPI) -> None:
    api.send_message(1, "x" * 5000, html=False)
    (call,) = fake.sent("sendMessage")
    assert len(call.params["text"]) == 4096 and "parse_mode" not in call.params


def test_send_photo_is_multipart(fake: FakeTelegram, api: TelegramAPI, tmp_path: Path) -> None:
    png = tmp_path / "c.png"
    png.write_bytes(b"\x89PNG fake")
    api.send_photo(5, png, caption="chart")
    (call,) = fake.sent("sendPhoto")
    assert call.files["photo"] == b"\x89PNG fake"
    assert call.params["chat_id"] == "5" and call.params["caption"] == "chart"


def test_get_updates_params(fake: FakeTelegram, api: TelegramAPI) -> None:
    fake.queue(fake.message(1, "hello"))
    updates = api.get_updates(offset=5, timeout_sec=1)
    assert updates[0]["message"]["text"] == "hello"
    params = fake.sent("getUpdates")[0].params
    assert params["offset"] == 5 and params["allowed_updates"] == ["message", "callback_query"]


def test_api_error_is_token_free(fake: FakeTelegram, api: TelegramAPI) -> None:
    fake.failures["sendMessage"] = (400, f"Bad Request: echo {FAKE_TOKEN}")
    with pytest.raises(TelegramError) as info:
        api.send_message(1, "x")
    assert FAKE_TOKEN not in str(info.value) and REDACTED in str(info.value)


def test_network_error_is_token_free(fake: FakeTelegram, api: TelegramAPI) -> None:
    url = f"https://api.telegram.org/bot{FAKE_TOKEN}/getUpdates"
    fake.raise_on["getUpdates"] = httpx.ConnectError(f"cannot reach {url}")
    with pytest.raises(TelegramError) as info:
        api.get_updates(None, 1)
    assert FAKE_TOKEN not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_missing_token_rejected() -> None:
    with pytest.raises(TelegramError):
        TelegramAPI("")


def test_redacting_filter_and_quiet_loggers(caplog: pytest.LogCaptureFixture) -> None:
    quiet_http_loggers()
    for name in ("httpx", "httpcore", "httpx2"):  # httpx2: the anthropic SDK's copy
        assert logging.getLogger(name).level == logging.WARNING
    log = logging.getLogger("scrollback.test")
    caplog.handler.addFilter(RedactingFilter(FAKE_TOKEN))
    with caplog.at_level(logging.INFO):
        log.info("url /bot%s/getUpdates", FAKE_TOKEN)
    assert FAKE_TOKEN not in caplog.text and REDACTED in caplog.text
    assert redact("abc", "") == "abc"
