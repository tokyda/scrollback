"""A small Telegram Bot API client over httpx (DESIGN §8).

Only the calls the bot needs: getUpdates (long polling), sendMessage, sendPhoto,
sendChatAction and answerCallbackQuery. Tests pass an ``httpx.MockTransport``.

**The token is a secret.** It is part of every request URL (``/bot<token>/<method>``), so:
- httpx and httpcore loggers are raised to WARNING (at INFO they log each URL);
- every error raised here has the token replaced by ``<token>``;
- :class:`RedactingFilter` scrubs it from any log record as a second line of defence.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

API_BASE = "https://api.telegram.org"
REDACTED = "<token>"
MESSAGE_LIMIT = 4096  # Telegram's maximum message length (characters)


class TelegramError(RuntimeError):
    """A Bot API call failed. The message never contains the token."""


def redact(text: str, token: str) -> str:
    return text.replace(token, REDACTED) if token else text


class RedactingFilter(logging.Filter):
    """Replaces the token in every log record that passes through a handler."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._token:
            return True
        try:
            message = record.getMessage()
        except Exception:  # a malformed record; leave it to the handler
            return True
        if self._token in message:
            record.msg, record.args = redact(message, self._token), None
        if record.exc_text and self._token in record.exc_text:
            record.exc_text = redact(record.exc_text, self._token)
        return True


# httpx2 is the anthropic SDK's own httpx copy (API URLs only, but the same noise).
HTTP_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2")


def quiet_http_loggers() -> None:
    """httpx logs every request URL (with the token) at INFO; keep it to warnings."""
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


class TelegramAPI:
    def __init__(
        self,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_sec: float = 60.0,
        base_url: str = API_BASE,
    ) -> None:
        if not token:
            raise TelegramError("no bot token")
        quiet_http_loggers()  # always: at INFO httpx logs each URL, token included
        self._token = token
        self._client = httpx.Client(
            base_url=f"{base_url}/bot{token}/", timeout=timeout_sec, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ core call

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        timeout_sec: float | None = None,
    ) -> Any:
        """POST a Bot API method and return its ``result``; errors are token-free."""
        kwargs: dict[str, Any] = {}
        if timeout_sec is not None:
            kwargs["timeout"] = timeout_sec
        try:
            if files:
                # multipart: nested values (reply_markup) must be JSON strings
                data = {
                    k: (json.dumps(v) if isinstance(v, dict | list) else str(v))
                    for k, v in (params or {}).items()
                }
                response = self._client.post(method, data=data, files=files, **kwargs)
            else:
                response = self._client.post(method, json=params or {}, **kwargs)
        except httpx.HTTPError as exc:
            raise TelegramError(
                redact(f"{method}: {type(exc).__name__}: {exc}", self._token)
            ) from None
        try:
            payload = response.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {response.status_code}, not JSON") from None
        if not isinstance(payload, dict) or not payload.get("ok"):
            desc = payload.get("description", "") if isinstance(payload, dict) else ""
            raise TelegramError(
                redact(f"{method}: HTTP {response.status_code}: {desc}", self._token)
            )
        return payload.get("result")

    # ------------------------------------------------------------------ methods

    def get_updates(self, offset: int | None, timeout_sec: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout_sec,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            params["offset"] = offset
        result = self.call("getUpdates", params, timeout_sec=timeout_sec + 15)
        return list(result or [])

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        html: bool = True,
        reply_markup: dict[str, Any] | None = None,
        reply_to: int | None = None,
    ) -> int:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text[:MESSAGE_LIMIT]}
        if html:
            params["parse_mode"] = "HTML"
        if reply_markup:
            params["reply_markup"] = reply_markup
        if reply_to is not None:
            params["reply_parameters"] = {
                "message_id": reply_to,
                "allow_sending_without_reply": True,
            }
        return int(self.call("sendMessage", params)["message_id"])

    def send_photo(
        self, chat_id: int, path: Path, *, caption: str | None = None, reply_to: int | None = None
    ) -> int:
        params: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            params["caption"] = caption[:1024]
        if reply_to is not None:
            params["reply_parameters"] = {
                "message_id": reply_to,
                "allow_sending_without_reply": True,
            }
        files = {"photo": (path.name, path.read_bytes(), "image/png")}
        return int(self.call("sendPhoto", params, files=files)["message_id"])

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.call("sendChatAction", {"chat_id": chat_id, "action": action})

    def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        """The command menu Telegram shows when you type "/"."""
        self.call(
            "setMyCommands",
            {"commands": [{"command": c, "description": d} for c, d in commands]},
        )

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text
        self.call("answerCallbackQuery", params)
