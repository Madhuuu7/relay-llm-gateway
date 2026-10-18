from __future__ import annotations

import pytest

from relay.breaker import BreakerConfig, BreakerState, CircuitBreaker


class FakeClock:
    """Time under test control: a breaker is a state machine over time, and
    sleeping through real cooldowns would make this suite take minutes."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def breaker(clock: FakeClock, **overrides) -> CircuitBreaker:
    return CircuitBreaker("p", BreakerConfig(**overrides), clock)


def test_starts_closed_and_passes_traffic(clock: FakeClock) -> None:
    cb = breaker(clock)
    assert cb.state is BreakerState.CLOSED
    assert cb.allow()


def test_consecutive_failures_trip_it(clock: FakeClock) -> None:
    cb = breaker(clock, failure_threshold=3)
    for _ in range(2):
        cb.on_failure()
    assert cb.state is BreakerState.CLOSED
    cb.on_failure()
    assert cb.state is BreakerState.OPEN
    assert not cb.allow()
    assert cb.trips == 1


def test_a_success_resets_the_failure_run(clock: FakeClock) -> None:
    """Three failures spread around a success is not three in a row."""
    cb = breaker(clock, failure_threshold=3)
    cb.on_failure()
    cb.on_failure()
    cb.on_success()
    cb.on_failure()
    cb.on_failure()
    assert cb.state is BreakerState.CLOSED


def test_sustained_error_rate_trips_it_without_a_failure_run(clock: FakeClock) -> None:
    """One failure in three never trips a counter but is still unusable."""
    cb = breaker(clock, failure_threshold=10, error_rate_threshold=0.3, min_requests=20)
    cb.on_failure(error_rate=0.34, requests=60)
    assert cb.state is BreakerState.OPEN


def test_rate_rule_waits_for_enough_traffic(clock: FakeClock) -> None:
    cb = breaker(clock, failure_threshold=10, error_rate_threshold=0.3, min_requests=20)
    cb.on_failure(error_rate=1.0, requests=3)  # one bad call out of three
    assert cb.state is BreakerState.CLOSED


def test_cooldown_moves_it_to_half_open(clock: FakeClock) -> None:
    cb = breaker(clock, failure_threshold=1, cooldown_seconds=10.0)
    cb.on_failure()
    assert cb.state is BreakerState.OPEN
    assert cb.cooldown_remaining == pytest.approx(10.0)

    clock.advance(9.0)
    assert cb.state is BreakerState.OPEN
    assert not cb.allow()

    clock.advance(1.5)
    assert cb.state is BreakerState.HALF_OPEN


def test_half_open_admits_only_bounded_probes(clock: FakeClock) -> None:
    """Reopening to full load is how a recovering provider falls over again."""
    cb = breaker(clock, failure_threshold=1, cooldown_seconds=5.0, max_probes=1)
    cb.on_failure()
    clock.advance(6.0)

    assert cb.allow()  # the one probe
    assert not cb.allow()  # everything else still rejected
    assert not cb.allow()
    assert cb.rejected == 2


def test_probes_must_succeed_repeatedly_to_close(clock: FakeClock) -> None:
    cb = breaker(clock, failure_threshold=1, cooldown_seconds=5.0, probe_successes=2)
    cb.on_failure()
    clock.advance(6.0)

    assert cb.allow()
    cb.on_success()
    assert cb.state is BreakerState.HALF_OPEN  # one is not enough

    assert cb.allow()
    cb.on_success()
    assert cb.state is BreakerState.CLOSED
    assert cb.allow()


def test_a_failed_probe_reopens_with_a_longer_cooldown(clock: FakeClock) -> None:
    """Backing off stops the gateway relearning the same fact every 10s."""
    cb = breaker(clock, failure_threshold=1, cooldown_seconds=10.0)
    cb.on_failure()

    clock.advance(11.0)
    assert cb.allow()
    cb.on_failure()
    assert cb.state is BreakerState.OPEN
    assert cb.cooldown_remaining == pytest.approx(20.0)

    clock.advance(21.0)
    assert cb.allow()
    cb.on_failure()
    assert cb.cooldown_remaining == pytest.approx(40.0)


def test_backoff_is_capped(clock: FakeClock) -> None:
    cb = breaker(clock, failure_threshold=1, cooldown_seconds=10.0, max_cooldown_seconds=45.0)
    cb.on_failure()
    for _ in range(6):
        clock.advance(cb.cooldown_remaining + 1.0)
        assert cb.allow()
        cb.on_failure()
    assert cb.cooldown_remaining == pytest.approx(45.0)


def test_closing_resets_the_cooldown(clock: FakeClock) -> None:
    cb = breaker(clock, failure_threshold=1, cooldown_seconds=10.0, probe_successes=1)
    cb.on_failure()
    clock.advance(11.0)
    assert cb.allow()
    cb.on_failure()  # cooldown is now 20s

    clock.advance(21.0)
    assert cb.allow()
    cb.on_success()
    assert cb.state is BreakerState.CLOSED

    cb.on_failure()
    assert cb.cooldown_remaining == pytest.approx(10.0)


def test_probe_slot_is_released_on_either_outcome(clock: FakeClock) -> None:
    """A leaked probe slot means the breaker never closes again."""
    cb = breaker(clock, failure_threshold=1, cooldown_seconds=5.0, probe_successes=5)
    cb.on_failure()
    clock.advance(6.0)
    for _ in range(4):
        assert cb.allow(), "probe slot was not released"
        cb.on_success()
    assert cb.state is BreakerState.HALF_OPEN


def test_reset_is_available_for_operators(clock: FakeClock) -> None:
    cb = breaker(clock, failure_threshold=1)
    cb.on_failure()
    assert cb.state is BreakerState.OPEN
    cb.reset()
    assert cb.state is BreakerState.CLOSED
    assert cb.allow()


def test_snapshot_reports_state(clock: FakeClock) -> None:
    cb = breaker(clock, failure_threshold=1, cooldown_seconds=30.0)
    cb.on_failure()
    snapshot = cb.snapshot()
    assert snapshot["state"] == "open"
    assert snapshot["trips"] == 1
    assert float(snapshot["cooldown_remaining_s"]) == pytest.approx(30.0)
