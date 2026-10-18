"""How the gateway picks a provider, and in what order it falls back.

Four policies, three of which exist to be beaten:

* :class:`StaticPriority` -- the list in the config file. What almost every
  gateway ships: try A, then B, then C. Correct exactly as long as A is the
  best choice, which is to say until A has a bad five minutes.
* :class:`RoundRobin` -- spread load evenly. Fair, and indifferent to the fact
  that one provider is currently timing out.
* :class:`RandomChoice` -- the honest null hypothesis.
* :class:`Adaptive` -- rank by what the providers are doing *right now*.

Scoring, and the unit problem
-----------------------------
The adaptive score is a cost in **milliseconds-equivalent**, lower is better::

    score = p95_latency
          + p95_latency * error_rate * error_penalty
          + in_flight * queue_penalty
          + dollars_per_request * ms_per_dollar

Each term is a decision stated out loud:

* **p95, not mean.** A gateway exists to protect the tail. A provider
  averaging 400 ms with a 9 s p95 is worse than one flat at 900 ms, and the
  mean cannot see the difference.
* **Errors are priced as latency.** A failed call does not just not-work: it
  costs its own elapsed time *plus* a retry somewhere else. Scaling the error
  rate by this provider's own p95 is a defensible estimate of that, and it
  makes a flaky-but-fast provider comparable to a reliable-but-slow one on one
  axis.
* **Queue depth.** Without it, every concurrent request picks the same
  provider before a single result comes back, and the gateway builds its own
  thundering herd.
* **Cost needs an exchange rate.** Dollars cannot be added to milliseconds
  without declaring what a second of latency is worth. ``ms_per_dollar``
  is that declaration -- the default says one dollar per million requests is
  worth about a millisecond -- and setting it to zero gives pure
  latency routing. Pretending the question does not exist is how gateways end
  up with a cost term that silently dominates.

Exploration
-----------
A provider the router stops choosing stops being measured, so its score
freezes at whatever it was during its worst minute and it is never picked
again -- even after it recovers. The circuit breaker handles hard failures;
this handles *slow* ones. So a fraction ``epsilon`` of requests ignore the
ranking and try someone else, which is the price of keeping the estimates
alive. Unmeasured providers are explored first, before random ones.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from typing import Protocol, Sequence

from ..breaker import BreakerState, CircuitBreaker
from ..pricing import PriceBook
from ..providers.base import CompletionRequest, Provider
from ..stats import ProviderHealth


@dataclass
class Candidate:
    """A provider plus everything known about it."""

    provider: Provider
    health: ProviderHealth
    breaker: CircuitBreaker
    priority: int = 0
    """Position in the configured static order; ties broken by it."""

    @property
    def name(self) -> str:
        return self.provider.name

    @property
    def available(self) -> bool:
        return self.breaker.state is not BreakerState.OPEN


class Policy(Protocol):
    name: str

    def rank(
        self, candidates: Sequence[Candidate], request: CompletionRequest
    ) -> list[Candidate]:
        """Candidates in the order they should be attempted."""

    def explain(self, candidates: Sequence[Candidate]) -> list[dict[str, object]]:
        """Per-candidate scoring detail, for the dashboard."""


@dataclass
class StaticPriority:
    """Configured order, every time. The baseline to beat."""

    name: str = "static"

    def rank(
        self, candidates: Sequence[Candidate], request: CompletionRequest
    ) -> list[Candidate]:
        return sorted(candidates, key=lambda c: c.priority)

    def explain(self, candidates: Sequence[Candidate]) -> list[dict[str, object]]:
        return [
            {"provider": c.name, "score": float(c.priority), "reason": "configured order"}
            for c in self.rank(candidates, _PROBE)
        ]


@dataclass
class RoundRobin:
    """Even split, regardless of health."""

    name: str = "round_robin"
    _tick: itertools.count = field(default_factory=itertools.count)

    def rank(
        self, candidates: Sequence[Candidate], request: CompletionRequest
    ) -> list[Candidate]:
        ordered = sorted(candidates, key=lambda c: c.name)
        if not ordered:
            return []
        offset = next(self._tick) % len(ordered)
        return ordered[offset:] + ordered[:offset]

    def explain(self, candidates: Sequence[Candidate]) -> list[dict[str, object]]:
        return [
            {"provider": c.name, "score": 0.0, "reason": "round robin"}
            for c in sorted(candidates, key=lambda c: c.name)
        ]


@dataclass
class RandomChoice:
    """Uniform random. The null hypothesis the others must beat."""

    name: str = "random"
    seed: int | None = None
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def rank(
        self, candidates: Sequence[Candidate], request: CompletionRequest
    ) -> list[Candidate]:
        shuffled = list(candidates)
        self._rng.shuffle(shuffled)
        return shuffled

    def explain(self, candidates: Sequence[Candidate]) -> list[dict[str, object]]:
        return [{"provider": c.name, "score": 0.0, "reason": "random"} for c in candidates]


@dataclass(frozen=True)
class AdaptiveConfig:
    tail_weight: float = 0.25
    """How much of the latency term is the p95 rather than the p50.

    1.0 is pure tail protection: a provider with a wide spread is avoided even
    for the median request. 0.0 routes on typical latency and lets the tail
    fall where it may.

    The default is **measured, not guessed**: ``scripts/replay.py --sweep``
    walks this knob over the benchmark trace, and 0.25 is where the curve
    turns -- it keeps ~96% of the p95 improvement that full tail-weighting
    buys while giving back most of the median latency and nearly all of the
    extra cost that pure tail routing spends."""

    error_penalty: float = 3.0
    """How many of its own p95s a provider's failure is assumed to cost."""

    queue_penalty_ms: float = 60.0
    """Added per request already in flight to that provider."""

    ms_per_dollar: float = 1_000_000.0
    """Exchange rate between cost and latency. 0 disables cost routing."""

    min_samples: int = 8
    """Below this, a provider's score is treated as unknown, not good."""

    epsilon: float = 0.05
    """Fraction of requests that deliberately ignore the ranking."""

    optimism_ms: float = 150.0
    """Score assigned to an unmeasured provider, so it gets tried early."""


@dataclass
class Adaptive:
    """Rank by live p95, error rate, queue depth and price."""

    config: AdaptiveConfig = field(default_factory=AdaptiveConfig)
    prices: PriceBook = field(default_factory=PriceBook)
    seed: int | None = None
    name: str = "adaptive"
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    # ------------------------------------------------------------------
    def score(self, candidate: Candidate) -> float:
        config = self.config
        health = candidate.health

        if health.requests < config.min_samples:
            # Optimism under uncertainty: an untried provider is cheap to try
            # once, and expensive to never try at all.
            return config.optimism_ms + candidate.priority

        p95 = health.latency_p95.value
        p50 = health.latency_p50.value
        latency = config.tail_weight * p95 + (1.0 - config.tail_weight) * p50

        score = latency
        # A failure costs its own elapsed time *and* a retry elsewhere, so it
        # is priced in this provider's own latency rather than as a flat
        # constant -- that keeps flaky-but-fast comparable to slow-but-solid.
        score += latency * health.error_rate.value * config.error_penalty
        score += health.in_flight * config.queue_penalty_ms
        score += self._cost_term(candidate)
        return score

    def _cost_term(self, candidate: Candidate) -> float:
        if not self.config.ms_per_dollar:
            return 0.0
        health = candidate.health
        if health.requests == 0 or health.total_cost_usd <= 0.0:
            return 0.0
        per_request = health.total_cost_usd / health.requests
        return per_request * self.config.ms_per_dollar

    def rank(
        self, candidates: Sequence[Candidate], request: CompletionRequest
    ) -> list[Candidate]:
        if not candidates:
            return []

        ordered = sorted(candidates, key=lambda c: (self.score(c), c.priority, c.name))
        if len(ordered) == 1 or self._rng.random() >= self.config.epsilon:
            return ordered

        # Exploration. Prefer a provider nobody has measured; otherwise pick
        # any non-leader, so the leader's crown is defended rather than
        # inherited.
        unmeasured = [
            c for c in ordered[1:] if c.health.requests < self.config.min_samples
        ]
        pool = unmeasured or ordered[1:]
        chosen = self._rng.choice(pool)
        rest = [c for c in ordered if c is not chosen]
        return [chosen, *rest]

    def explain(self, candidates: Sequence[Candidate]) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for candidate in sorted(candidates, key=lambda c: self.score(c)):
            health = candidate.health
            warm = health.requests >= self.config.min_samples
            out.append(
                {
                    "provider": candidate.name,
                    "score": round(self.score(candidate), 1),
                    "p50_ms": round(health.latency_p50.value, 1),
                    "p95_ms": round(health.latency_p95.value, 1),
                    "error_rate": round(health.error_rate.value, 4),
                    "in_flight": health.in_flight,
                    "cost_term_ms": round(self._cost_term(candidate), 1),
                    "breaker": candidate.breaker.state.value,
                    "reason": "measured" if warm else "optimistic (unmeasured)",
                }
            )
        return out


_PROBE = CompletionRequest(messages=(), request_id="explain")


POLICIES: dict[str, type] = {
    "static": StaticPriority,
    "round_robin": RoundRobin,
    "random": RandomChoice,
    "adaptive": Adaptive,
}


def build_policy(name: str, **kwargs: object) -> Policy:
    try:
        factory = POLICIES[name]
    except KeyError:
        raise ValueError(
            f"unknown policy {name!r}; choose from {sorted(POLICIES)}"
        ) from None
    return factory(**kwargs)  # type: ignore[arg-type]
