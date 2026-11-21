"""An in-memory Telegram Bot API for tests (httpx.MockTransport). No network."""

from __future__ import annotations

import email
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

FAKE_TOKEN = "123456:FAKE-token-ABCdefGHIjkl_must_never_leak"


@dataclass
class Call:
    method: str
    params: dict[str, Any]
    files: dict[str, bytes] = field(default_factory=dict)


class FakeTelegram:
    def __init__(self, token: str = FAKE_TOKEN) -> None:
        self.token = token
        self.calls: list[Call] = []
        self.update_batches: list[list[dict[str, Any]]] = []
        self.failures: dict[str, tuple[int, str]] = {}  # method -> (status, description)
        self.raise_on: dict[str, Exception] = {}
        self.next_message_id = 1000
        self.next_update_id = 1

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # ------------------------------------------------------------------ building updates

    def message(self, chat_id: int, text: str, reply_to: int | None = None) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "message_id": self._mid(),
            "chat": {"id": chat_id, "type": "private"},
            "date": 0,
            "text": text,
        }
        if text.startswith("/"):
            cmd = text.split()[0]
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(cmd)}]
        if reply_to is not None:
            msg["reply_to_message"] = {"message_id": reply_to, "chat": msg["chat"], "date": 0}
        return self._update(message=msg)

    def callback(self, chat_id: int, data: str, message_id: int = 1) -> dict[str, Any]:
        cq = {
            "id": f"cq{self.next_update_id}",
            "from": {"id": chat_id},
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat_id}, "date": 0},
        }
        return self._update(callback_query=cq)

    def queue(self, *updates: dict[str, Any]) -> None:
        self.update_batches.append(list(updates))

    def sent(self, method: str) -> list[Call]:
        return [c for c in self.calls if c.method == method]

    # ------------------------------------------------------------------ internals

    def _mid(self) -> int:
        self.next_message_id += 1
        return self.next_message_id

    def _update(self, **body: Any) -> dict[str, Any]:
        update = {"update_id": self.next_update_id, **body}
        self.next_update_id += 1
        return update

    def _handle(self, request: httpx.Request) -> httpx.Response:
        prefix = f"/bot{self.token}/"
        assert request.url.path.startswith(prefix), "wrong token or base URL"
        method = request.url.path[len(prefix) :]
        if method in self.raise_on:
            raise self.raise_on[method]
        params, files = self._parse(request)
        self.calls.append(Call(method, params, files))
        if method in self.failures:
            status, desc = self.failures[method]
            return httpx.Response(status, json={"ok": False, "description": desc})
        return httpx.Response(200, json={"ok": True, "result": self._result(method, params)})

    def _parse(self, request: httpx.Request) -> tuple[dict[str, Any], dict[str, bytes]]:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("application/json"):
            return json.loads(request.content or b"{}"), {}
        msg = email.message_from_bytes(
            b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + request.content
        )
        params: dict[str, Any] = {}
        files: dict[str, bytes] = {}
        for part in msg.walk():
            if part.is_multipart():
                continue
            name = part.get_param("name", header="content-disposition")
            raw = part.get_payload(decode=True)
            payload = raw if isinstance(raw, bytes) else b""
            if part.get_filename():
                files[str(name)] = payload
            else:
                params[str(name)] = payload.decode()
        return params, files

    def _result(self, method: str, params: dict[str, Any]) -> Any:
        if method == "getUpdates":
            offset = params.get("offset")
            if offset is not None and int(offset) < 0:  # "drop pending": last update only
                pending = [u for b in self.update_batches for u in b]
                self.update_batches = []
                return pending[-1:] if pending else []
            return self.update_batches.pop(0) if self.update_batches else []
        if method in ("sendMessage", "sendPhoto"):
            return {"message_id": self._mid(), "chat": {"id": params.get("chat_id")}, "date": 0}
        return True
