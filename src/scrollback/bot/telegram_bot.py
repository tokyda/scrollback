"""The Telegram bot (DESIGN §8): questions in, answers + charts + "Show SQL" out.

- Only chats in ``TELEGRAM_ALLOWED_CHAT_IDS`` get any reaction. Every other update (message or
  button press) is dropped silently: no reply, no callback answer, and the log counts it
  without the chat ID.
- Free text runs the agent with the interactive config (``interactive.experiment``, DESIGN
  §6.7). A chart is requested only when the text contains a ``bot.chart_keywords`` word, and
  for ``/week``; a chart the agent makes on its own is sent too.
- ``/today`` and ``/week`` are fixed agent questions about the *most recent* data, because an
  export always ends before the day it is used. ``/metrics`` lists ``metrics.yml`` (no LLM).
- Every answer carries 👍 / 👎 buttons (``fb:<rating>:<trace_id>``). 👎 then asks for an
  optional note as a reply. ``/feedback good|bad [note]`` rates the answer it replies to, or
  the chat's last answer.
- ``aggregate_only`` is required: the bot refuses to start in any other result mode.

Logs carry trace IDs, statuses, costs and error *types* only: no question text, answers, query
results, chat IDs or the token.
"""

from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from scrollback.agent.llm import LLMClient
from scrollback.agent.loop import STATUS_MESSAGES, AgentResult, run_agent
from scrollback.bot.telegram_api import MESSAGE_LIMIT, TelegramAPI, TelegramError
from scrollback.config import Settings
from scrollback.feedback import (
    RATINGS,
    Feedback,
    MessageMap,
    Rating,
    find_trace,
    record_feedback,
)

log = logging.getLogger("scrollback.bot")

TODAY_QUESTION = (
    "How much did I watch on the most recent day in my data? State the date you used in the answer."
)
# Weekday names come from dim_date: Haiku worked them out itself and got all seven wrong by
# one on the phone check (2026-11-22). Bot-level only; the system prompt is unchanged.
WEEK_QUESTION = (
    "Show my watch time for the last 7 days of data, by day, with a chart. "
    "Take weekday names from dim_date.dow_name; don't work them out. "
    "State which dates you used."
)
HELP_TEXT = (
    "Ask me anything about your TikTok watch history, e.g. "
    "<i>how many minutes did I watch last Sunday?</i>\n"
    "Say <i>chart</i> (or plot, graph, trend) to get a chart.\n\n"
    "/today: watch time on the most recent day in your data\n"
    "/week: the last 7 days of data, by day, with a chart\n"
    "/metrics: what each metric means\n"
    "/feedback good|bad [note]: rate an answer (reply to it, or it rates the last one)"
)
FEEDBACK_USAGE = (
    "Usage: <code>/feedback good|bad [note]</code>. Reply to an answer to rate that one; "
    "otherwise it rates the last answer here."
)
BOT_COMMANDS = [
    ("today", "Watch time on the most recent day in your data"),
    ("week", "The last 7 days of data, by day, with a chart"),
    ("metrics", "What each metric means"),
    ("feedback", "good|bad [note]: rate an answer (reply to it) or the last one"),
    ("help", "What I can do"),
]
SQL_CALLBACK = "sql:"
RATE_CALLBACK = "fb:"  # fb:good:<trace_id> / fb:bad:<trace_id>
NOTE_PLACEHOLDER = "What was wrong? (optional)"
SQL_CHARS = 3500  # leaves room for the HTML wrapper inside Telegram's 4096 limit
TEXT_BUDGET = MESSAGE_LIMIT - 96


def wants_chart(text: str, keywords: list[str]) -> bool:
    words = set(re.findall(r"[a-z]+", text.lower()))
    return any(k.lower() in words for k in keywords)


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _footer(result: AgentResult) -> str:
    t = result.trace
    cost = f"${t.cost_usd:.4f}" if t.cost_usd is not None else "cost n/a"
    model = ", ".join(t.models_served) or t.model
    return f"<code>{t.trace_id}</code> · {_esc(model)} · {cost} · {t.latency_ms / 1000:.1f}s"


def format_reply(result: AgentResult) -> str:
    """HTML reply: answer, assumptions (incl. resolved dates), metrics, trace footer."""
    a = result.answer
    if result.status != "answered" or a is None:
        message = STATUS_MESSAGES.get(result.status, f"No answer ({result.status}).")
        return f"{_esc(message)}\n\n{_footer(result)}"
    tail: list[str] = []
    if a.assumptions:
        tail.append("<b>Assumptions</b>")
        tail += [f"• {_esc(x)}" for x in a.assumptions]
    if a.metrics_used:
        tail.append(f"<b>Metrics:</b> {_esc(', '.join(a.metrics_used))}")
    if not a.sql_used:
        tail.append("<i>No query behind this answer.</i>")
    tail.append(_footer(result))
    tail_text = "\n".join(tail)
    body = _esc(a.text)
    room = TEXT_BUDGET - len(tail_text) - 2
    if len(body) > room:
        body = body[: max(room - 12, 0)] + " …(cut)"
    return f"{body}\n\n{tail_text}"


def answer_markup(trace_id: str, *, has_sql: bool) -> dict[str, Any]:
    """Inline buttons under every answer: 👍 / 👎 (and Show SQL when a query ran)."""
    rows = [
        [
            {"text": "👍", "callback_data": f"{RATE_CALLBACK}good:{trace_id}"},
            {"text": "👎", "callback_data": f"{RATE_CALLBACK}bad:{trace_id}"},
        ]
    ]
    if has_sql:
        rows.insert(0, [{"text": "Show SQL", "callback_data": f"{SQL_CALLBACK}{trace_id}"}])
    return {"inline_keyboard": rows}


def _toast(prefix: str, question: str) -> str:
    """Callback answer text (Telegram shows up to 200 characters)."""
    text = f"{prefix} on “{question}”" if question else prefix
    return text if len(text) <= 200 else text[:199] + "…"


def pack(blocks: list[str], limit: int = TEXT_BUDGET) -> list[str]:
    """Join blocks with blank lines into as few messages as fit under ``limit``."""
    messages: list[str] = []
    current = ""
    for block in blocks:
        block = block[:limit]
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def metrics_blocks(settings: Settings) -> list[str]:
    from scrollback.semantic.metrics import load_metrics, render

    mf = load_metrics()
    blocks = [
        "<b>Metrics</b> (from knowledge/metrics.yml, with this bot's parameters)\n"
        + _esc(render(mf.watch_time_method.summary.strip(), settings))
    ]
    for m in mf.metrics:
        blocks.append(
            f"<b>{_esc(render(m.label, settings))}</b> <code>{m.name}</code>\n"
            + _esc(render(m.description.strip(), settings))
        )
    return blocks


class ScrollbackBot:
    def __init__(
        self,
        api: TelegramAPI,
        settings: Settings,
        llm: LLMClient,
        *,
        db_path: Path | None = None,
        today: Callable[[], date] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if settings.agent.result_mode != "aggregate_only":
            raise ValueError("the bot runs only with agent.result_mode = aggregate_only")
        self.allowed = frozenset(settings.secrets.telegram_allowed_chat_ids)
        if not self.allowed:
            raise ValueError("TELEGRAM_ALLOWED_CHAT_IDS is empty: the bot would answer no one")
        self.api = api
        self.settings = settings
        self.llm = llm
        self.db_path = db_path or settings.paths.warehouse
        self.traces_dir = settings.paths.traces_dir
        self.messages = MessageMap(self.traces_dir)
        self._today = today
        self._sleep = sleep
        self.ignored = 0

    # ------------------------------------------------------------------ polling

    def skip_pending(self) -> int | None:
        """Offset past updates sent while the bot was off, or None when there are none."""
        pending = self.api.get_updates(offset=-1, timeout_sec=0)
        if not pending:
            return None
        log.info("skipped messages sent while the bot was off")
        return int(pending[-1]["update_id"]) + 1

    def register_commands(self) -> None:
        try:
            self.api.set_my_commands(BOT_COMMANDS)
        except TelegramError as exc:  # the menu is a convenience; keep running
            log.warning("setMyCommands failed: %s", exc)

    def run(self, *, max_polls: int | None = None) -> None:
        self.register_commands()
        offset = self.skip_pending() if self.settings.bot.drop_pending_on_start else None
        polls, backoff = 0, 1.0
        while max_polls is None or polls < max_polls:
            polls += 1
            try:
                updates = self.api.get_updates(offset, self.settings.bot.poll_timeout_sec)
            except TelegramError as exc:
                log.warning("getUpdates failed: %s", exc)
                self._sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            backoff = 1.0
            for update in updates:
                offset = int(update["update_id"]) + 1
                self.safe_handle(update)

    def safe_handle(self, update: dict[str, Any]) -> None:
        try:
            self.handle_update(update)
        except TelegramError as exc:
            log.warning("reply failed: %s", exc)
        except Exception as exc:  # keep polling; log the type only (no data)
            log.error("update failed: %s", type(exc).__name__)

    # ------------------------------------------------------------------ dispatch

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._on_callback(update["callback_query"])
            return
        msg = update.get("message")
        if not isinstance(msg, dict):
            return
        chat_id = msg.get("chat", {}).get("id")
        if chat_id not in self.allowed:
            self._ignore()
            return
        text = (msg.get("text") or "").strip()
        message_id = int(msg["message_id"])
        if not text:
            self.api.send_message(chat_id, "Send me a question as text.", reply_to=message_id)
            return
        if text.startswith("/"):
            self._on_command(chat_id, message_id, text, msg)
            return
        replied = msg.get("reply_to_message")
        if isinstance(replied, dict):
            noted = self.messages.note_prompt_trace(chat_id, int(replied["message_id"]))
            if noted is not None:
                self._on_note(chat_id, message_id, noted, text)
                return
        want = wants_chart(text, self.settings.bot.chart_keywords)
        self.answer(chat_id, text, want_chart=want, reply_to=message_id)

    def _ignore(self) -> None:
        self.ignored += 1
        log.info("ignored an update from a chat not on the allowlist (%d so far)", self.ignored)

    def _on_command(self, chat_id: int, message_id: int, text: str, msg: dict[str, Any]) -> None:
        head, _, rest = text.partition(" ")
        command = head.split("@", 1)[0].lower()
        if command in ("/start", "/help"):
            self.api.send_message(chat_id, HELP_TEXT)
        elif command == "/today":
            self.answer(chat_id, TODAY_QUESTION, want_chart=False, reply_to=message_id)
        elif command == "/week":
            self.answer(chat_id, WEEK_QUESTION, want_chart=True, reply_to=message_id)
        elif command == "/metrics":
            for part in pack(metrics_blocks(self.settings)):
                self.api.send_message(chat_id, part)
        elif command == "/feedback":
            self._on_feedback(chat_id, message_id, rest, msg)
        elif command == "/wrapped":
            self.api.send_message(chat_id, "/wrapped isn't built yet (stretch).")
        else:
            self.api.send_message(chat_id, f"Unknown command.\n\n{HELP_TEXT}")

    # ------------------------------------------------------------------ answers

    def answer(self, chat_id: int, question: str, *, want_chart: bool, reply_to: int) -> None:
        try:
            self.api.send_chat_action(chat_id)
        except TelegramError as exc:
            log.warning("sendChatAction failed: %s", exc)
        try:
            result = run_agent(
                question,
                settings=self.settings,
                llm=self.llm,
                db_path=self.db_path,
                today=self._today() if self._today else None,
                want_chart=want_chart,
            )
        except Exception as exc:
            log.error("agent run failed: %s", type(exc).__name__)
            self.api.send_message(
                chat_id,
                f"Sorry, that failed before an answer ({_esc(type(exc).__name__)}). "
                "Nothing was answered.",
                reply_to=reply_to,
            )
            return
        trace = result.trace
        log.info(
            "trace %s · %s · %d steps · %s",
            trace.trace_id,
            result.status,
            trace.steps,
            f"${trace.cost_usd:.4f}" if trace.cost_usd is not None else "cost n/a",
        )
        if result.trace_db_error:
            log.warning("%s", result.trace_db_error)
        a = result.answer
        markup = answer_markup(trace.trace_id, has_sql=a is not None and bool(a.sql_used))
        mid = self.api.send_message(
            chat_id, format_reply(result), reply_markup=markup, reply_to=reply_to
        )
        self.messages.add(chat_id, reply_to, trace.trace_id)  # the question itself
        self.messages.add(chat_id, mid, trace.trace_id)
        if a is not None and a.chart_path and Path(a.chart_path).exists():
            pid = self.api.send_photo(chat_id, Path(a.chart_path), reply_to=mid)
            self.messages.add(chat_id, pid, trace.trace_id)

    # ------------------------------------------------------------------ Show SQL

    def _on_callback(self, cq: dict[str, Any]) -> None:
        message = cq.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        if chat_id not in self.allowed:
            self._ignore()
            return
        data = str(cq.get("data") or "")
        if data.startswith(RATE_CALLBACK):
            self._on_rate_button(chat_id, str(cq["id"]), data, message.get("message_id"))
            return
        self.api.answer_callback_query(str(cq["id"]))
        if not data.startswith(SQL_CALLBACK):
            return
        trace_id = data[len(SQL_CALLBACK) :]
        record = find_trace(self.traces_dir, trace_id)
        final = (record or {}).get("final_answer") or {}
        sqls = [str(s) for s in final.get("sql_used") or []]
        reply_to = message.get("message_id")
        if not sqls:
            self.api.send_message(chat_id, "No SQL found for that answer.", reply_to=reply_to)
            return
        for i, sql in enumerate(sqls, 1):
            shown = sql if len(sql) <= SQL_CHARS else sql[:SQL_CHARS] + "\n-- …(truncated)"
            header = f"<b>Query {i} of {len(sqls)}</b>\n" if len(sqls) > 1 else ""
            sql_id = self.api.send_message(
                chat_id,
                f'{header}<pre><code class="language-sql">{_esc(shown)}</code></pre>',
                reply_to=reply_to,
            )
            self.messages.add(chat_id, sql_id, trace_id)

    # ------------------------------------------------------------------ 👍 / 👎

    def _on_rate_button(
        self, chat_id: int, callback_id: str, data: str, answer_id: int | None
    ) -> None:
        rating, _, trace_id = data[len(RATE_CALLBACK) :].partition(":")
        if rating not in RATINGS or find_trace(self.traces_dir, trace_id) is None:
            self.api.answer_callback_query(callback_id, "I can't find that answer any more.")
            return
        fb = self._record(trace_id, rating)  # rating narrowed by the RATINGS check
        question = self._question_of(trace_id)
        if fb.rating == "good":
            self.api.answer_callback_query(callback_id, _toast("Recorded 👍", question))
            return
        self.api.answer_callback_query(callback_id, _toast("Recorded 👎", question))
        prompt_id = self.api.send_message(
            chat_id,
            f"Recorded 👎 on {self._quote(trace_id)}. What was wrong? Reply to this message "
            "with a note, or ignore it. It will be a candidate eval question.",
            reply_markup={"force_reply": True, "input_field_placeholder": NOTE_PLACEHOLDER},
            reply_to=answer_id,
        )
        self.messages.add_note_prompt(chat_id, prompt_id, trace_id)

    def _on_note(self, chat_id: int, message_id: int, trace_id: str, note: str) -> None:
        self._record(trace_id, "bad", note)
        self.api.send_message(
            chat_id, f"Thanks, note added to 👎 on {self._quote(trace_id)}.", reply_to=message_id
        )

    def _record(self, trace_id: str, rating: Rating, note: str = "") -> Feedback:
        fb, db_error = record_feedback(
            self.traces_dir, self.db_path, trace_id=trace_id, rating=rating, note=note
        )
        if db_error:
            log.warning("%s", db_error)
        log.info("feedback %s on trace %s%s", fb.rating, fb.trace_id, " (note)" if note else "")
        return fb

    def _question_of(self, trace_id: str) -> str:
        record = find_trace(self.traces_dir, trace_id) or {}
        asked = str(record.get("question") or "")
        return asked if len(asked) <= 200 else asked[:200] + "…"

    def _quote(self, trace_id: str) -> str:
        asked = self._question_of(trace_id)
        code = f"<code>{trace_id}</code>"
        return f"“{_esc(asked)}” ({code})" if asked else code

    # ------------------------------------------------------------------ feedback

    def _on_feedback(self, chat_id: int, message_id: int, rest: str, msg: dict[str, Any]) -> None:
        rating, _, note = rest.strip().partition(" ")
        rating = rating.lower()
        if rating not in RATINGS:
            self.api.send_message(chat_id, FEEDBACK_USAGE, reply_to=message_id)
            return
        replied = msg.get("reply_to_message")
        if isinstance(replied, dict):
            trace_id = self.messages.trace_for(chat_id, int(replied["message_id"]))
            if trace_id is None:
                self.api.send_message(
                    chat_id,
                    "That message isn't linked to an answer. Reply to your question, my "
                    "answer, its chart or its SQL, or send /feedback without replying to rate "
                    "the last answer.",
                    reply_to=message_id,
                )
                return
        else:
            trace_id = self.messages.last_trace(chat_id)
            if trace_id is None:
                self.api.send_message(
                    chat_id, "Nothing to rate yet: ask a question first.", reply_to=message_id
                )
                return
        fb = self._record(trace_id, rating, note)  # rating narrowed by the RATINGS check
        extra = " It will be a candidate eval question." if fb.rating == "bad" else ""
        # Quote the rated question, so a rating that landed on the wrong answer shows at once.
        self.api.send_message(
            chat_id,
            f"Thanks, recorded <b>{fb.rating}</b> feedback on {self._quote(trace_id)}.{extra}",
            reply_to=message_id,
        )
