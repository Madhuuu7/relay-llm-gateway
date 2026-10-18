"""The gateway: pick a provider, call it, fail over, and record what happened.

Everything the router knows comes from this loop, so the order of operations
matters more than it looks:

1. **Rank** the candidates with the policy.
2. **Gate** each one on its circuit breaker. An open breaker is skipped
   without a call, which is the whole point of having one.
3. **Call** with a per-attempt timeout that is also clipped to the remaining
   total budget -- otherwise three attempts at a 20 s timeout turn a 10 s
   deadline into a minute.
4. **Record** the outcome into health *and* the breaker, success or failure,
   exactly once. A missed recording is worse than a wrong one: the breaker
   leaks a probe slot and never closes again.

Fail-over is bounded by both an attempt count and a time budget, and stops
immediately on a fatal error -- a malformed request will be malformed at every
provider, so trying them all just multiplies one client mistake into three
bills.

Budget accounting uses the *attempts' own* latencies rather than a wall clock.
A simulated replay has no wall clock to read (it advances time by arithmetic),
and for live traffic the two agree, so one code path serves both.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Callable, Sequence

from ..breaker import BreakerConfig, BreakerState, CircuitBreaker
from ..pricing import PriceBook
from ..providers.base import (
    Completion,
    CompletionRequest,
    Provider,
    ProviderError,
)
from ..stats import ProviderHealth
from .policies import Adaptive, Candidate, Policy


@dataclass(frozen=True)
class Attempt:
    """One call, or one deliberate non-call."""

    provider: str
    ok: bool
    latency_ms: float = 0.0
    error: str | None = None
    error_kind: str | None = None
    skipped: str | None = None

    @property
    def called(self) -> bool:
        return self.skipped is None


@dataclass(frozen=True)
class GatewayResult:
    request_id: str
    policy: str
    attempts: tuple[Attempt, ...]
    completion: Completion | None = None
    latency_ms: float = 0.0
    """Total latency the *client* saw, including failed attempts."""

    @property
    def ok(self) -> bool:
        return self.completion is not None

    @property
    def provider(self) -> str | None:
        return self.completion.provider if self.completion else None

    @property
    def failovers(self) -> int:
        return max(0, sum(1 for a in self.attempts if a.called) - 1)

    @property
    def cost_usd(self) -> float:
        return self.completion.cost_usd if self.completion else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "policy": self.policy,
            "ok": self.ok,
            "provider": self.provider,
            "model": self.completion.model if self.completion else None,
            "latency_ms": round(self.latency_ms, 1),
            "cost_usd": round(self.cost_usd, 8),
            "input_tokens": self.completion.usage.input_tokens if self.completion else 0,
            "output_tokens": self.completion.usage.output_tokens if self.completion else 0,
            "failovers": self.failovers,
            "attempts": [
                {
                    "provider": a.provider,
                    "ok": a.ok,
                    "latency_ms": round(a.latency_ms, 1),
                    "error": a.error,
                    "error_kind": a.error_kind,
                    "skipped": a.skipped,
                }
                for a in self.attempts
            ],
        }


class GatewayError(Exception):
    """Every candidate failed, was skipped, or the budget ran out."""

    def __init__(self, message: str, result: GatewayResult) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class GatewayConfig:
    attempt_timeout: float = 20.0
    total_budget: float = 45.0
    max_attempts: int = 3


class Gateway:
    """Routes completions over a set of providers."""

    def __init__(
        self,
        providers: Sequence[Provider],
        policy: Policy | None = None,
        *,
        config: GatewayConfig | None = None,
        breaker_config: BreakerConfig | None = None,
        prices: PriceBook | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_result: Callable[[GatewayResult], None] | None = None,
    ) -> None:
        if not providers:
            raise ValueError("a gateway needs at least one provider")
        self.config = config or GatewayConfig()
        self.prices = prices or PriceBook()
        self.policy: Policy = policy or Adaptive(prices=self.prices)
        self.clock = clock
        self.on_result = on_result
        self._breaker_config = breaker_config or BreakerConfig()

        self.candidates: list[Candidate] = [
            Candidate(
                provider=provider,
                health=ProviderHealth(provider.name),
                breaker=CircuitBreaker(provider.name, self._breaker_config, clock),
                priority=index,
            )
            for index, provider in enumerate(providers)
        ]
        self._by_name = {c.name: c for c in self.candidates}
        self.served = 0
        self.failed = 0

    # ------------------------------------------------------------------
    def candidate(self, name: str) -> Candidate:
        return self._by_name[name]

    async def complete(self, request: CompletionRequest) -> GatewayResult:
        """Serve one request, failing over as needed.

        Raises :class:`GatewayError` when nothing could serve it; the
        exception carries the full attempt list so the caller can log *why*
        rather than just "failed".
        """
        attempts: list[Attempt] = []
        spent_ms = 0.0
        budget_ms = self.config.total_budget * 1000.0
        calls = 0

        for candidate in self.policy.rank(self.candidates, request):
            if calls >= self.config.max_attempts:
                break

            remaining_ms = budget_ms - spent_ms
            if remaining_ms <= 0:
                attempts.append(
                    Attempt(candidate.name, ok=False, skipped="budget_exhausted")
                )
                break

            if not candidate.breaker.allow():
                attempts.append(
                    Attempt(
                        candidate.name,
                        ok=False,
                        skipped=f"breaker_{candidate.breaker.state.value}",
                    )
                )
                continue

            timeout = min(self.config.attempt_timeout, remaining_ms / 1000.0)
            calls += 1
            candidate.health.in_flight += 1
            started = self.clock()
            try:
                completion = await candidate.provider.complete(request, timeout=timeout)
            except ProviderError as error:
                elapsed = (
                    error.elapsed_ms
                    if error.elapsed_ms is not None
                    else (self.clock() - started) * 1000.0
                )
                candidate.health.in_flight -= 1
                candidate.health.record_failure(elapsed)
                candidate.breaker.on_failure(
                    error_rate=candidate.health.error_rate.value,
                    requests=candidate.health.requests,
                )
                spent_ms += elapsed
                attempts.append(
                    Attempt(
                        candidate.name,
                        ok=False,
                        latency_ms=elapsed,
                        error=str(error),
                        error_kind=type(error).__name__,
                    )
                )
                if error.fatal:
                    break
                continue
            except Exception as error:  # noqa: BLE001 - an adapter bug is still an outage
                elapsed = (self.clock() - started) * 1000.0
                candidate.health.in_flight -= 1
                candidate.health.record_failure(elapsed)
                candidate.breaker.on_failure(
                    error_rate=candidate.health.error_rate.value,
                    requests=candidate.health.requests,
                )
                spent_ms += elapsed
                attempts.append(
                    Attempt(
                        candidate.name,
                        ok=False,
                        latency_ms=elapsed,
                        error=f"adapter raised {type(error).__name__}: {error}",
                        error_kind=type(error).__name__,
                    )
                )
                continue

            candidate.health.in_flight -= 1
            # Providers report their own latency so a simulated run can report
            # simulated time; live adapters measure it off the wire.
            latency = completion.latency_ms or (self.clock() - started) * 1000.0
            cost = completion.cost_usd or self.prices.cost(
                completion.model, completion.usage
            )
            completion = replace(completion, latency_ms=latency, cost_usd=cost)

            candidate.health.record_success(latency, cost, completion.usage.total_tokens)
            candidate.breaker.on_success()
            spent_ms += latency
            attempts.append(Attempt(candidate.name, ok=True, latency_ms=latency))

            result = GatewayResult(
                request_id=request.request_id,
                policy=self.policy.name,
                attempts=tuple(attempts),
                completion=completion,
                latency_ms=spent_ms,
            )
            self.served += 1
            if self.on_result:
                self.on_result(result)
            return result

        result = GatewayResult(
            request_id=request.request_id,
            policy=self.policy.name,
            attempts=tuple(attempts),
            completion=None,
            latency_ms=spent_ms,
        )
        self.failed += 1
        if self.on_result:
            self.on_result(result)
        raise GatewayError(_why(attempts), result)

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, object]:
        """Everything the dashboard shows."""
        return {
            "policy": self.policy.name,
            "served": self.served,
            "failed": self.failed,
            "providers": [
                {
                    **c.health.snapshot(),
                    "breaker": c.breaker.snapshot(),
                    "models": list(c.provider.models),
                }
                for c in self.candidates
            ],
            "ranking": self.policy.explain(self.candidates),
        }

    async def aclose(self) -> None:
        for candidate in self.candidates:
            await candidate.provider.aclose()


def _why(attempts: Sequence[Attempt]) -> str:
    if not attempts:
        return "no provider was eligible"
    parts = []
    for attempt in attempts:
        if attempt.skipped:
            parts.append(f"{attempt.provider} skipped ({attempt.skipped})")
        else:
            parts.append(f"{attempt.provider} failed ({attempt.error})")
    return "; ".join(parts)
