"""Per-provider circuit breaker.

Retrying a dead provider is worse than not having it. Every request that goes
to a provider which is down pays its full timeout before failing over, so the
gateway's latency becomes the broken provider's timeout -- and the failover
traffic arrives at the healthy providers in a burst, late.

The breaker is a three-state machine:

    CLOSED  --- too many failures ---> OPEN
    OPEN    --- cooldown elapsed  ---> HALF_OPEN
    HALF_OPEN --- probe succeeds  ---> CLOSED   (after `probe_successes`)
    HALF_OPEN --- probe fails     ---> OPEN     (with a longer cooldown)

Two details that matter more than the diagram:

**Half-open admits a bounded number of probes, not all traffic.** Reopening to
full load is how a recovering provider gets knocked over again, and how a
thundering herd forms behind a shared dependency. One request at a time finds
out whether it is back.

**The cooldown backs off.** A provider that fails its probe is probably still
broken; doubling the wait each time (to a cap) stops the gateway from
spending a request every few seconds to learn the same thing.

Opening is driven by *consecutive* failures and, separately, by a sustained
error rate once there is enough traffic to judge: a provider failing one call
in three never trips a consecutive-failure counter but is still unusable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class BreakerConfig:
    failure_threshold: int = 5
    """Consecutive failures that trip the breaker."""

    error_rate_threshold: float = 0.5
    """Sustained (EWMA) error rate that trips it, once warm."""

    min_requests: int = 20
    """Requests before the rate rule is allowed to fire."""

    cooldown_seconds: float = 10.0
    """How long to stay open before probing."""

    max_cooldown_seconds: float = 300.0
    """Ceiling for the doubling backoff."""

    probe_successes: int = 2
    """Consecutive probe successes needed to close again."""

    max_probes: int = 1
    """Concurrent probes allowed while half-open."""


@dataclass
class CircuitBreaker:
    """One breaker per provider. Not thread-safe by design: the gateway owns
    it inside a single event loop, and a lock here would serialise the whole
    routing decision."""

    name: str
    config: BreakerConfig = field(default_factory=BreakerConfig)
    clock: Callable[[], float] = time.monotonic

    _state: BreakerState = BreakerState.CLOSED
    _consecutive_failures: int = 0
    _probe_successes: int = 0
    _probes_in_flight: int = 0
    _opened_at: float = 0.0
    _cooldown: float = 0.0
    _trips: int = 0
    _rejected: int = 0

    def __post_init__(self) -> None:
        self._cooldown = self.config.cooldown_seconds

    # ------------------------------------------------------------------
    @property
    def state(self) -> BreakerState:
        """Current state, moving OPEN -> HALF_OPEN when the cooldown is up.

        Reading the state performs that transition because nothing else wakes
        a breaker up: there is no timer thread, the next request to look is
        what discovers that the cooldown expired.
        """
        if self._state is BreakerState.OPEN:
            if self.clock() - self._opened_at >= self._cooldown:
                self._state = BreakerState.HALF_OPEN
                self._probe_successes = 0
                self._probes_in_flight = 0
        return self._state

    @property
    def trips(self) -> int:
        return self._trips

    @property
    def rejected(self) -> int:
        return self._rejected

    @property
    def cooldown_remaining(self) -> float:
        if self._state is not BreakerState.OPEN:
            return 0.0
        return max(0.0, self._cooldown - (self.clock() - self._opened_at))

    # ------------------------------------------------------------------
    def allow(self) -> bool:
        """Whether a request may be sent now.

        Reserves a probe slot when half-open, so this must be paired with
        exactly one :meth:`on_success` or :meth:`on_failure`; otherwise the
        breaker leaks probe slots and never closes again.
        """
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.OPEN:
            self._rejected += 1
            return False
        if self._probes_in_flight >= self.config.max_probes:
            self._rejected += 1
            return False
        self._probes_in_flight += 1
        return True

    def on_success(self) -> None:
        self._consecutive_failures = 0
        if self._state is BreakerState.HALF_OPEN:
            self._probes_in_flight = max(0, self._probes_in_flight - 1)
            self._probe_successes += 1
            if self._probe_successes >= self.config.probe_successes:
                self._close()

    def on_failure(self, error_rate: float = 0.0, requests: int = 0) -> None:
        """Record a failure. ``error_rate``/``requests`` come from the
        provider's health so the rate rule can fire without the breaker
        keeping its own counters twice."""
        if self._state is BreakerState.HALF_OPEN:
            # A failed probe means still broken; wait longer before the next.
            self._probes_in_flight = max(0, self._probes_in_flight - 1)
            self._open(backoff=True)
            return

        self._consecutive_failures += 1
        tripped = self._consecutive_failures >= self.config.failure_threshold
        sustained = (
            requests >= self.config.min_requests
            and error_rate >= self.config.error_rate_threshold
        )
        if self._state is BreakerState.CLOSED and (tripped or sustained):
            self._open(backoff=False)

    def reset(self) -> None:
        """Force closed. For operator intervention and for tests."""
        self._close()

    # ------------------------------------------------------------------
    def _open(self, *, backoff: bool) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self.clock()
        self._trips += 1
        self._probe_successes = 0
        self._probes_in_flight = 0
        if backoff:
            self._cooldown = min(
                self._cooldown * 2.0, self.config.max_cooldown_seconds
            )
        else:
            self._cooldown = self.config.cooldown_seconds

    def _close(self) -> None:
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._probe_successes = 0
        self._probes_in_flight = 0
        self._cooldown = self.config.cooldown_seconds

    def snapshot(self) -> dict[str, object]:
        return {
            "provider": self.name,
            "state": self.state.value,
            "trips": self._trips,
            "rejected": self._rejected,
            "consecutive_failures": self._consecutive_failures,
            "cooldown_remaining_s": round(self.cooldown_remaining, 2),
        }
