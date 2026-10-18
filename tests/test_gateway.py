from __future__ import annotations

import pytest

from relay.breaker import BreakerConfig, BreakerState
from relay.pricing import PriceBook
from relay.providers.base import (
    BadRequest,
    ChatMessage,
    Completion,
    CompletionRequest,
    RateLimited,
    Unavailable,
    Usage,
)
from relay.routing.gateway import Gateway, GatewayConfig, GatewayError
from relay.routing.policies import StaticPriority


def request(index: int = 1) -> CompletionRequest:
    return CompletionRequest(
        messages=[ChatMessage("user", "hi")], request_id=f"t{index:05d}"
    )


class Fake:
    """A provider that does exactly what the test tells it to."""

    def __init__(
        self,
        name: str,
        *,
        fail: Exception | None = None,
        latency_ms: float = 100.0,
        model: str = "sim-fast",
        usage: Usage | None = None,
        cost_usd: float = 0.0,
    ) -> None:
        self.name = name
        self.fail = fail
        self.latency_ms = latency_ms
        self.model = model
        self.usage = usage or Usage(500, 100)
        self.cost_usd = cost_usd
        self.calls = 0
        self.timeouts: list[float] = []
        self.closed = False

    @property
    def models(self):
        return (self.model,)

    async def complete(self, req, *, timeout):
        self.calls += 1
        self.timeouts.append(timeout)
        if self.fail is not None:
            raise self.fail
        return Completion(
            text="ok",
            model=self.model,
            provider=self.name,
            usage=self.usage,
            latency_ms=self.latency_ms,
            cost_usd=self.cost_usd,
        )

    async def aclose(self) -> None:
        self.closed = True


def gateway(*providers, **kwargs) -> Gateway:
    kwargs.setdefault("policy", StaticPriority())
    kwargs.setdefault("prices", PriceBook())
    return Gateway(list(providers), **kwargs)


# ----------------------------------------------------------------------
def test_requires_at_least_one_provider() -> None:
    with pytest.raises(ValueError, match="at least one provider"):
        Gateway([], StaticPriority())


async def test_serves_from_the_first_healthy_provider() -> None:
    primary, backup = Fake("primary"), Fake("backup")
    result = await gateway(primary, backup).complete(request())

    assert result.ok
    assert result.provider == "primary"
    assert backup.calls == 0
    assert result.failovers == 0


async def test_fails_over_on_a_retryable_error() -> None:
    broken = Fake("broken", fail=Unavailable("503", provider="broken", elapsed_ms=50.0))
    backup = Fake("backup")
    result = await gateway(broken, backup).complete(request())

    assert result.provider == "backup"
    assert result.failovers == 1
    assert [a.ok for a in result.attempts] == [False, True]
    assert result.attempts[0].error_kind == "Unavailable"


async def test_rate_limits_fail_over() -> None:
    limited = Fake("limited", fail=RateLimited("429", provider="limited", elapsed_ms=5.0))
    result = await gateway(limited, Fake("backup")).complete(request())
    assert result.provider == "backup"


async def test_a_fatal_error_is_not_retried_anywhere() -> None:
    """A malformed request is malformed at every provider; trying them all
    turns one client mistake into three bills."""
    bad = Fake("bad", fail=BadRequest("max_tokens must be positive", provider="bad"))
    backup = Fake("backup")
    gw = gateway(bad, backup)

    with pytest.raises(GatewayError) as caught:
        await gw.complete(request())

    assert backup.calls == 0
    assert "max_tokens" in str(caught.value)
    assert caught.value.result.ok is False


async def test_an_open_breaker_is_skipped_without_a_call() -> None:
    down = Fake("down", fail=Unavailable("503", provider="down", elapsed_ms=10.0))
    backup = Fake("backup")
    gw = gateway(down, backup, breaker_config=BreakerConfig(failure_threshold=1))

    await gw.complete(request(1))  # trips the breaker on `down`
    assert gw.candidate("down").breaker.state is BreakerState.OPEN

    calls_before = down.calls
    result = await gw.complete(request(2))
    assert down.calls == calls_before, "open breaker must not issue a call"
    assert result.attempts[0].skipped == "breaker_open"
    assert result.provider == "backup"


async def test_attempts_are_capped() -> None:
    one = Fake("one", fail=Unavailable("x", elapsed_ms=1.0))
    two = Fake("two", fail=Unavailable("x", elapsed_ms=1.0))
    three = Fake("three")
    gw = gateway(one, two, three, config=GatewayConfig(max_attempts=2))

    with pytest.raises(GatewayError):
        await gw.complete(request())
    assert three.calls == 0


async def test_the_attempt_timeout_is_clipped_to_the_remaining_budget() -> None:
    """Three attempts at a 20s timeout must not turn a 10s deadline into 60s."""
    slow = Fake("slow", fail=Unavailable("timeout", elapsed_ms=8_000.0))
    backup = Fake("backup")
    gw = gateway(
        slow, backup, config=GatewayConfig(attempt_timeout=20.0, total_budget=10.0)
    )

    await gw.complete(request())
    assert slow.timeouts[0] == pytest.approx(10.0)
    assert backup.timeouts[0] == pytest.approx(2.0, abs=0.01)


async def test_budget_exhaustion_stops_the_loop() -> None:
    burner = Fake("burner", fail=Unavailable("timeout", elapsed_ms=11_000.0))
    backup = Fake("backup")
    gw = gateway(burner, backup, config=GatewayConfig(total_budget=10.0))

    with pytest.raises(GatewayError) as caught:
        await gw.complete(request())
    assert backup.calls == 0
    assert caught.value.result.attempts[-1].skipped == "budget_exhausted"


async def test_health_and_breaker_see_every_outcome_exactly_once() -> None:
    flaky = Fake("flaky", fail=Unavailable("503", elapsed_ms=40.0))
    backup = Fake("backup")
    gw = gateway(flaky, backup)

    for i in range(3):
        await gw.complete(request(i))

    bad = gw.candidate("flaky").health
    good = gw.candidate("backup").health
    assert bad.requests == 3 and bad.failures == 3
    assert good.requests == 3 and good.failures == 0
    assert bad.error_rate.value > 0.9


async def test_in_flight_returns_to_zero_on_both_paths() -> None:
    """A leaked in-flight count permanently biases the queue-depth term."""
    flaky = Fake("flaky", fail=Unavailable("503", elapsed_ms=5.0))
    gw = gateway(flaky, Fake("backup"))
    await gw.complete(request())
    assert all(c.health.in_flight == 0 for c in gw.candidates)


async def test_an_adapter_bug_is_treated_as_an_outage() -> None:
    """A TypeError inside an adapter must fail over, not 500 the gateway."""

    class Broken(Fake):
        async def complete(self, req, *, timeout):
            self.calls += 1
            raise TypeError("adapter is wrong")

    broken = Broken("broken")
    result = await gateway(broken, Fake("backup")).complete(request())
    assert result.provider == "backup"
    assert "adapter raised TypeError" in (result.attempts[0].error or "")


async def test_cost_is_filled_in_when_the_provider_does_not_price() -> None:
    provider = Fake("p", model="claude-haiku-5-5", usage=Usage(1_000_000, 1_000_000))
    result = await gateway(provider).complete(request())
    # haiku 5.5: $0.10 in + $0.50 out per 1M tokens
    assert result.cost_usd == pytest.approx(0.60)


async def test_a_provider_reported_cost_is_respected() -> None:
    provider = Fake("p", model="claude-haiku-5-5", cost_usd=0.123)
    result = await gateway(provider).complete(request())
    assert result.cost_usd == pytest.approx(0.123)


async def test_unpriced_models_are_recorded_not_guessed() -> None:
    prices = PriceBook()
    provider = Fake("p", model="some-new-model")
    result = await gateway(provider, prices=prices).complete(request())
    assert result.cost_usd == 0.0
    assert "some-new-model" in prices.unpriced


async def test_on_result_fires_for_success_and_failure() -> None:
    seen = []
    gw = gateway(
        Fake("down", fail=Unavailable("503", elapsed_ms=1.0)),
        on_result=seen.append,
    )
    with pytest.raises(GatewayError):
        await gw.complete(request(1))
    gw2 = gateway(Fake("up"), on_result=seen.append)
    await gw2.complete(request(2))

    assert [r.ok for r in seen] == [False, True]
    assert seen[0].as_dict()["ok"] is False


async def test_result_serialises_for_the_api() -> None:
    result = await gateway(Fake("p", model="claude-sonnet-5-5")).complete(request())
    body = result.as_dict()
    assert body["provider"] == "p"
    assert body["model"] == "claude-sonnet-5-5"
    assert body["attempts"][0]["ok"] is True
    assert isinstance(body["latency_ms"], float)


async def test_snapshot_reports_providers_breakers_and_ranking() -> None:
    gw = gateway(Fake("a"), Fake("b"))
    await gw.complete(request())
    snapshot = gw.snapshot()
    assert snapshot["policy"] == "static"
    assert snapshot["served"] == 1
    assert {p["provider"] for p in snapshot["providers"]} == {"a", "b"}
    assert all("breaker" in p for p in snapshot["providers"])
    assert len(snapshot["ranking"]) == 2


async def test_aclose_closes_every_provider() -> None:
    a, b = Fake("a"), Fake("b")
    gw = gateway(a, b)
    await gw.aclose()
    assert a.closed and b.closed
