"""LLM client interface, the Anthropic implementation and a scripted mock (DESIGN §6.1).

The agent loop only talks to :class:`LLMClient`. Tests use :class:`ScriptedLLMClient`, which
returns pre-written turns, so the loop, retries and step limit are tested without network.

The Anthropic client:
- reads the key from settings (``ANTHROPIC_API_KEY`` in ``.env``); it is never logged, printed
  or written to a trace;
- sends no sampling parameters (current models reject them) and sets ``effort`` instead;
- uses tool_choice ``auto`` (forced tool use is rejected by current models), so the loop must
  cope with a turn that calls no tool;
- appends each assistant turn back unchanged, which current models require for their thinking
  blocks;
- uses automatic prompt caching; the system prompt and tools are byte-stable across questions.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from scrollback.config import Settings


class LLMError(RuntimeError):
    """The model could not be called (auth, rate limit, network, bad request)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
            self.cache_creation_input_tokens + other.cache_creation_input_tokens,
        )

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )


@dataclass
class LLMResponse:
    text: str  # concatenated text blocks (may be empty)
    tool_calls: list[ToolCall]
    stop_reason: str  # end_turn | tool_use | max_tokens | refusal | ...
    usage: Usage
    model: str  # the model that actually served the request
    assistant_content: Any  # appended to the conversation verbatim
    latency_ms: float = 0.0
    request_id: str | None = None


class LLMClient(Protocol):
    model: str

    def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse: ...


# --------------------------------------------------------------------------- Anthropic


class AnthropicLLMClient:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.model = settings.llm.model
        self.effort = settings.llm.effort
        self.max_tokens = settings.llm.max_tokens
        if client is not None:
            self._client = client
            return
        key = settings.secrets.anthropic_api_key
        if key is None or not key.get_secret_value():
            raise LLMError("config", "ANTHROPIC_API_KEY is not set; add it to .env")
        import anthropic

        self._client = anthropic.Anthropic(
            api_key=key.get_secret_value(), timeout=settings.llm.timeout_sec, max_retries=2
        )

    def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        import anthropic

        started = time.perf_counter()
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=messages,
                tools=tools,
                tool_choice={"type": "auto"},
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
            )
        except anthropic.AuthenticationError:
            raise LLMError("auth", "the API key was rejected") from None
        except anthropic.PermissionDeniedError:
            raise LLMError("permission", "the API key lacks permission for this model") from None
        except anthropic.NotFoundError:
            raise LLMError("not_found", f"model {self.model!r} not found") from None
        except anthropic.RateLimitError:
            raise LLMError("rate_limit", "rate limited; try again shortly") from None
        except anthropic.BadRequestError as exc:
            raise LLMError("bad_request", str(exc.message)) from None
        except anthropic.APIStatusError as exc:
            raise LLMError("api", f"HTTP {exc.status_code}") from None
        except anthropic.APIConnectionError:
            raise LLMError("connection", "could not reach the API") from None
        latency_ms = (time.perf_counter() - started) * 1000
        return response_from_message(message, latency_ms)


def response_from_message(message: Any, latency_ms: float = 0.0) -> LLMResponse:
    """Convert an SDK ``Message`` into an :class:`LLMResponse`."""
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in message.content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type == "tool_use":
            calls.append(ToolCall(block.id, block.name, dict(block.input)))
    u = message.usage
    usage = Usage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_input_tokens=u.cache_read_input_tokens or 0,
        cache_creation_input_tokens=u.cache_creation_input_tokens or 0,
    )
    return LLMResponse(
        text="\n".join(texts).strip(),
        tool_calls=calls,
        stop_reason=str(message.stop_reason),
        usage=usage,
        model=str(message.model),
        assistant_content=message.content,
        latency_ms=latency_ms,
        request_id=getattr(message, "_request_id", None),
    )


# --------------------------------------------------------------------------- scripted mock

Turn = Callable[[list[dict[str, Any]]], LLMResponse] | LLMResponse
_mock_ids = itertools.count(1)


def mock_turn(
    *calls: tuple[str, dict[str, Any]],
    text: str = "",
    stop_reason: str | None = None,
    usage: Usage | None = None,
    model: str = "mock-model",
) -> LLMResponse:
    """Build a scripted assistant turn: optional text plus tool calls ``(name, input)``."""
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    tool_calls = []
    for name, tool_input in calls:
        call = ToolCall(id=f"toolu_mock_{next(_mock_ids)}", name=name, input=tool_input)
        tool_calls.append(call)
        content.append({"type": "tool_use", "id": call.id, "name": name, "input": tool_input})
    return LLMResponse(
        text=text,
        tool_calls=tool_calls,
        stop_reason=stop_reason or ("tool_use" if calls else "end_turn"),
        usage=usage or Usage(input_tokens=1000, output_tokens=100),
        model=model,
        assistant_content=content,
    )


@dataclass
class ScriptedLLMClient:
    """Returns scripted turns in order; a turn may be a function of the conversation so far."""

    turns: Sequence[Turn]
    model: str = "mock-model"
    requests: list[dict[str, Any]] = field(default_factory=list)

    def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        index = len(self.requests)
        self.requests.append(
            {"system": system, "messages": [dict(m) for m in messages], "tools": tools}
        )
        if index >= len(self.turns):
            raise AssertionError(f"script exhausted after {len(self.turns)} turns")
        turn = self.turns[index]
        return turn(messages) if callable(turn) else turn
