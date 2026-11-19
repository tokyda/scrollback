"""Estimated cost of LLM calls, recorded in every trace (DESIGN §6.6).

Prices are USD per million tokens, Anthropic first-party API rates as of PRICES_AS_OF.
Cache writes use the 5-minute TTL rate (1.25x input); cache reads use the listed read rate
(Sonnet 5.5: $0.10, Opus 5.5: $0.20; Haiku 5.5: 0.1x input). Update this table when prices
change; an unknown model yields ``None`` rather than a wrong number. The estimate uses the
model that served each call, so a different serving model is priced correctly.
"""

from __future__ import annotations

from dataclasses import dataclass

from scrollback.agent.llm import Usage

PRICES_AS_OF = "2026-11-03"


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_write: float
    cache_read: float


PRICES: dict[str, Price] = {
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_write=2.50, cache_read=0.10),
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_write=5.00, cache_read=0.20),
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_write=0.125, cache_read=0.01),
}


def estimate_cost(model: str, usage: Usage) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    return (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_creation_input_tokens * price.cache_write
        + usage.cache_read_input_tokens * price.cache_read
    ) / 1_000_000
