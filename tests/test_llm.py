"""LLM client: Anthropic request shape and response conversion (no network), mock, pricing."""

from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message
from pydantic import SecretStr

from scrollback.agent.llm import (
    AnthropicLLMClient,
    LLMError,
    ScriptedLLMClient,
    Usage,
    mock_turn,
    response_from_message,
)
from scrollback.agent.pricing import PRICES, estimate_cost
from tests.conftest import repo_settings


def _message(**overrides: Any) -> Message:
    data: dict[str, Any] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "toolu_1", "name": "run_sql", "input": {"sql": "SELECT 1"}},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 300,
            "cache_read_input_tokens": 4000,
            "cache_creation_input_tokens": 500,
        },
    }
    data.update(overrides)
    return Message.model_validate(data)


class FakeMessages:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeClient:
    def __init__(self, result: Any) -> None:
        self.messages = FakeMessages(result)


def test_request_shape() -> None:
    fake = FakeClient(_message())
    client = AnthropicLLMClient(repo_settings(), client=fake)
    client.complete(system="sys", messages=[{"role": "user", "content": "q"}], tools=[])
    kwargs = fake.messages.kwargs
    assert kwargs["model"] == "claude-sonnet-5-5"
    assert kwargs["output_config"] == {"effort": "medium"}
    assert kwargs["tool_choice"] == {"type": "auto"}
    assert kwargs["cache_control"] == {"type": "ephemeral"}
    assert kwargs["max_tokens"] == 16000
    for banned in ("temperature", "top_p", "top_k", "thinking", "fallbacks", "betas"):
        assert banned not in kwargs


def test_response_conversion() -> None:
    resp = response_from_message(_message(), latency_ms=12.5)
    assert resp.text == "Let me check."
    assert [(c.id, c.name, c.input) for c in resp.tool_calls] == [
        ("toolu_1", "run_sql", {"sql": "SELECT 1"})
    ]
    assert resp.stop_reason == "tool_use"
    assert resp.usage == Usage(1200, 300, 4000, 500)
    assert resp.model == "claude-sonnet-5-5"
    assert resp.assistant_content is not None and resp.latency_ms == 12.5


def test_missing_key_is_a_clear_error() -> None:
    settings = repo_settings()
    settings.secrets.anthropic_api_key = None
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY is not set"):
        AnthropicLLMClient(settings)


def _status_error(cls: type[anthropic.APIStatusError], status: int) -> Exception:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(status, request=request), body=None)


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (_status_error(anthropic.AuthenticationError, 401), "auth"),
        (_status_error(anthropic.RateLimitError, 429), "rate_limit"),
        (_status_error(anthropic.NotFoundError, 404), "not_found"),
        (_status_error(anthropic.InternalServerError, 500), "api"),
    ],
)
def test_errors_are_mapped_without_secrets(error: Exception, kind: str) -> None:
    settings = repo_settings()
    settings.secrets.anthropic_api_key = SecretStr("sk-ant-SECRET-VALUE")
    client = AnthropicLLMClient(settings, client=FakeClient(error))
    with pytest.raises(LLMError) as err:
        client.complete(system="s", messages=[], tools=[])
    assert err.value.kind == kind
    assert "SECRET" not in str(err.value)


def test_scripted_client_records_requests() -> None:
    client = ScriptedLLMClient([mock_turn(("run_sql", {"sql": "SELECT 1"})), mock_turn(text="x")])
    first = client.complete(system="s", messages=[{"role": "user", "content": "q"}], tools=[])
    assert first.tool_calls[0].name == "run_sql" and first.stop_reason == "tool_use"
    second = client.complete(system="s", messages=[], tools=[])
    assert second.stop_reason == "end_turn" and second.text == "x"
    assert len(client.requests) == 2
    with pytest.raises(AssertionError, match="script exhausted"):
        client.complete(system="s", messages=[], tools=[])


def test_pricing() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert estimate_cost("claude-sonnet-5-5", usage) == pytest.approx(12.0)
    assert estimate_cost("claude-opus-5-5", usage) == pytest.approx(24.0)
    cached = Usage(cache_read_input_tokens=1_000_000, cache_creation_input_tokens=1_000_000)
    assert estimate_cost("claude-sonnet-5-5", cached) == pytest.approx(0.10 + 2.50)
    assert estimate_cost("unknown-model", usage) is None
    assert "claude-sonnet-5-5" in PRICES
