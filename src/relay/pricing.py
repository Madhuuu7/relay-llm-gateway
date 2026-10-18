"""Per-model prices, and the cost of a single completion.

Prices are **configuration, not code**. Vendors change them, and a gateway
whose cost accounting is a literal in a function is wrong the week after a
price cut. The table below is a default, overridable with a JSON file via
``RELAY_PRICING``:

    {"gpt-4o-mini": {"input_per_mtok": 0.15, "output_per_mtok": 0.60}}

Anthropic rates are first-party API rates per million tokens, current as of
2026-10. Models not in the table cost 0.0 and are counted as unpriced rather
than guessed at -- a wrong number in a cost report is worse than a missing
one, because nobody checks a number that looks plausible.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .providers.base import Usage


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float

    def cost(self, usage: Usage) -> float:
        return (
            usage.input_tokens * self.input_per_mtok
            + usage.output_tokens * self.output_per_mtok
        ) / 1_000_000.0


# Anthropic first-party API rates (USD per 1M tokens).
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "claude-opus-5-5": ModelPrice(4.00, 20.00),
    "claude-opus-5": ModelPrice(5.00, 25.00),
    "claude-sonnet-5-5": ModelPrice(2.00, 10.00),
    "claude-sonnet-5": ModelPrice(2.00, 10.00),
    "claude-haiku-5-5": ModelPrice(0.10, 0.50),
    "claude-fable-5-1": ModelPrice(10.00, 50.00),
    # The synthetic provider used by the replay benchmark prices its own
    # models, so a simulated run reports simulated money, not nothing.
    "sim-fast": ModelPrice(0.50, 1.50),
    "sim-balanced": ModelPrice(1.00, 3.00),
    "sim-cheap": ModelPrice(0.10, 0.40),
}


class PriceBook:
    """Model id -> price, with unpriced models tracked rather than hidden."""

    def __init__(self, prices: dict[str, ModelPrice] | None = None) -> None:
        self._prices = dict(DEFAULT_PRICES if prices is None else prices)
        self.unpriced: set[str] = set()

    @classmethod
    def from_env(cls) -> PriceBook:
        book = cls()
        path = os.environ.get("RELAY_PRICING")
        if path:
            book.load(Path(path))
        return book

    def load(self, path: Path) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        for model, entry in raw.items():
            self._prices[model] = ModelPrice(
                float(entry["input_per_mtok"]), float(entry["output_per_mtok"])
            )

    def price_of(self, model: str) -> ModelPrice | None:
        return self._prices.get(model)

    def cost(self, model: str, usage: Usage) -> float:
        price = self._prices.get(model)
        if price is None:
            self.unpriced.add(model)
            return 0.0
        return price.cost(usage)

    def as_dict(self) -> dict[str, dict[str, float]]:
        return {
            model: {
                "input_per_mtok": price.input_per_mtok,
                "output_per_mtok": price.output_per_mtok,
            }
            for model, price in sorted(self._prices.items())
        }
