from __future__ import annotations

import pytest

from relay.breaker import BreakerConfig, CircuitBreaker
from relay.pricing import PriceBook
from relay.providers.base import ChatMessage, CompletionRequest
from relay.routing.policies import (
    Adaptive,
    AdaptiveConfig,
    Candidate,
    RandomChoice,
    RoundRobin,
    StaticPriority,
    build_policy,
)

REQUEST = CompletionRequest(messages=[ChatMessage("user", "hi")], request_id="t00001")


class StubProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    @property
    def models(self):
        return (f"{self.name}-model",)

    async def complete(self, request, *, timeout):  # pragma: no cover - never called
        raise AssertionError("policies must not call providers")

    async def aclose(self) -> None:
        return None


def candidate(
    name: str,
    *,
    priority: int = 0,
    p95: float | None = None,
    error_rate: float = 0.0,
    requests: int = 100,
    in_flight: int = 0,
    cost_per_request: float = 0.0,
) -> Candidate:
    cand = Candidate(
        provider=StubProvider(name),
        health=__import__("relay.stats", fromlist=["ProviderHealth"]).ProviderHealth(name),
        breaker=CircuitBreaker(name, BreakerConfig()),
        priority=priority,
    )
    if p95 is not None:
        for _ in range(requests):
            cand.health.latency_p95.add(p95)
            cand.health.latency_p50.add(p95)
    cand.health.requests = requests
    cand.health.error_rate.add(error_rate)
    cand.health.in_flight = in_flight
    cand.health.total_cost_usd = cost_per_request * requests
    return cand


def names(ordered) -> list[str]:
    return [c.name for c in ordered]


# ----------------------------------------------------------------------
def test_static_priority_keeps_the_configured_order() -> None:
    candidates = [candidate("c", priority=2), candidate("a", priority=0), candidate("b", priority=1)]
    assert names(StaticPriority().rank(candidates, REQUEST)) == ["a", "b", "c"]


def test_static_priority_ignores_health() -> None:
    """The baseline's defining weakness, asserted so the comparison is fair."""
    candidates = [
        candidate("primary", priority=0, p95=9_000.0, error_rate=0.5),
        candidate("backup", priority=1, p95=200.0),
    ]
    assert names(StaticPriority().rank(candidates, REQUEST))[0] == "primary"


def test_round_robin_rotates() -> None:
    policy = RoundRobin()
    candidates = [candidate("a"), candidate("b"), candidate("c")]
    first = names(policy.rank(candidates, REQUEST))
    second = names(policy.rank(candidates, REQUEST))
    third = names(policy.rank(candidates, REQUEST))
    assert first[0] == "a" and second[0] == "b" and third[0] == "c"
    assert sorted(first) == sorted(second) == ["a", "b", "c"]


def test_round_robin_still_returns_every_candidate() -> None:
    """Rotation picks the first choice; fail-over needs the rest behind it."""
    policy = RoundRobin()
    candidates = [candidate("a"), candidate("b")]
    assert len(policy.rank(candidates, REQUEST)) == 2


def test_random_is_seedable() -> None:
    candidates = [candidate("a"), candidate("b"), candidate("c")]
    one = names(RandomChoice(seed=5).rank(candidates, REQUEST))
    two = names(RandomChoice(seed=5).rank(candidates, REQUEST))
    assert one == two
    assert sorted(one) == ["a", "b", "c"]


# ----------------------------------------------------------------------
def _adaptive(**overrides) -> Adaptive:
    config = AdaptiveConfig(epsilon=0.0, **overrides)
    return Adaptive(config=config, prices=PriceBook(), seed=1)


def test_adaptive_prefers_the_faster_tail() -> None:
    candidates = [
        candidate("slow", p95=2_000.0),
        candidate("fast", p95=300.0),
    ]
    assert names(_adaptive().rank(candidates, REQUEST))[0] == "fast"


def test_adaptive_prices_errors_as_latency() -> None:
    """Fast but failing should lose to slower and reliable."""
    flaky = candidate("flaky", p95=300.0, error_rate=0.4)
    solid = candidate("solid", p95=600.0, error_rate=0.0)
    policy = _adaptive(error_penalty=3.0)
    assert policy.score(flaky) > policy.score(solid)
    assert names(policy.rank([flaky, solid], REQUEST))[0] == "solid"


def test_a_small_error_rate_does_not_outweigh_a_big_latency_gap() -> None:
    flaky = candidate("flaky", p95=300.0, error_rate=0.02)
    slow = candidate("slow", p95=3_000.0, error_rate=0.0)
    assert names(_adaptive().rank([flaky, slow], REQUEST))[0] == "flaky"


def test_queue_depth_spreads_concurrent_requests() -> None:
    """Without this every in-flight request piles onto one provider."""
    busy = candidate("busy", p95=300.0, in_flight=20)
    idle = candidate("idle", p95=400.0, in_flight=0)
    policy = _adaptive(queue_penalty_ms=60.0)
    assert names(policy.rank([busy, idle], REQUEST))[0] == "idle"


def test_unmeasured_providers_are_tried_before_bad_ones() -> None:
    fresh = candidate("fresh", p95=None, requests=0)
    known_bad = candidate("known_bad", p95=4_000.0)
    assert names(_adaptive().rank([fresh, known_bad], REQUEST))[0] == "fresh"


def test_unmeasured_providers_do_not_displace_a_good_one() -> None:
    """Optimism is bounded: a proven 150ms provider keeps the lead."""
    fresh = candidate("fresh", p95=None, requests=0)
    known_good = candidate("known_good", p95=90.0)
    assert names(_adaptive(optimism_ms=150.0).rank([fresh, known_good], REQUEST))[0] == "known_good"


def test_cost_breaks_a_latency_tie() -> None:
    cheap = candidate("cheap", p95=500.0, cost_per_request=0.0001)
    dear = candidate("dear", p95=500.0, cost_per_request=0.0040)
    policy = _adaptive(ms_per_dollar=1_000_000.0)
    assert names(policy.rank([cheap, dear], REQUEST))[0] == "cheap"


def test_cost_routing_can_be_switched_off() -> None:
    cheap = candidate("cheap", p95=900.0, cost_per_request=0.0001)
    dear = candidate("dear", p95=500.0, cost_per_request=0.0040)
    policy = _adaptive(ms_per_dollar=0.0)
    assert names(policy.rank([cheap, dear], REQUEST))[0] == "dear"
    assert policy._cost_term(dear) == 0.0  # noqa: SLF001


def test_exploration_sometimes_ignores_the_ranking() -> None:
    """A provider never chosen is never re-measured, so it can never recover."""
    best = candidate("best", p95=100.0)
    worse = candidate("worse", p95=800.0)
    policy = Adaptive(config=AdaptiveConfig(epsilon=0.5), prices=PriceBook(), seed=3)
    leaders = [names(policy.rank([best, worse], REQUEST))[0] for _ in range(60)]
    assert "worse" in leaders, "no exploration happened"
    assert leaders.count("best") > leaders.count("worse")


def test_exploration_is_off_when_epsilon_is_zero() -> None:
    best = candidate("best", p95=100.0)
    worse = candidate("worse", p95=800.0)
    policy = _adaptive()
    assert all(
        names(policy.rank([best, worse], REQUEST))[0] == "best" for _ in range(40)
    )


def test_explain_lists_every_candidate_with_a_reason() -> None:
    rows = _adaptive().explain([candidate("a", p95=200.0), candidate("b", p95=None, requests=0)])
    assert {row["provider"] for row in rows} == {"a", "b"}
    assert any("optimistic" in str(row["reason"]) for row in rows)
    assert all("score" in row and "breaker" in row for row in rows)


def test_empty_candidate_list_is_not_an_error() -> None:
    for policy in (StaticPriority(), RoundRobin(), RandomChoice(seed=1), _adaptive()):
        assert policy.rank([], REQUEST) == []


def test_build_policy_by_name() -> None:
    assert build_policy("round_robin").name == "round_robin"
    with pytest.raises(ValueError, match="unknown policy"):
        build_policy("vibes")
