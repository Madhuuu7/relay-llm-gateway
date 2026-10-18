"""Replay one trace through every routing policy and compare them.

The claim "adaptive routing beats a static fallback list" is only worth
anything as a number, and the number is only honest if the comparison is
controlled. So:

* **One trace.** The same N requests, in the same order, for every policy.
* **One set of provider behaviours.** Latency and failure for a given
  (provider, position) are hash-derived, so every policy sees the identical
  provider world. The only difference between two runs is where each request
  was sent -- which is the thing being measured.
* **Fresh state per policy.** Each run gets new health trackers and new
  breakers, so no policy inherits another's warm-up.

Scenario
--------
Three providers, drawn so that no static order is right for all of them: one
fast but flaky, one slower and dependable, one cheap with a bad tail. Partway
through the trace the fast one has an outage -- which is where a static list
pointed at it keeps paying the timeout, and where the breaker earns its place.

Usage::

    python scripts/replay.py
    python scripts/replay.py --requests 4000 --concurrency 16 --markdown
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from relay.breaker import BreakerConfig  # noqa: E402
from relay.pricing import PriceBook  # noqa: E402
from relay.providers.base import ChatMessage, CompletionRequest  # noqa: E402
from relay.providers.synthetic import Profile, SyntheticProvider  # noqa: E402
from relay.routing.gateway import Gateway, GatewayConfig, GatewayError  # noqa: E402
from relay.routing.policies import (  # noqa: E402
    Adaptive,
    AdaptiveConfig,
    RandomChoice,
    RoundRobin,
    StaticPriority,
)


@dataclass
class Outcome:
    policy: str
    latencies: list[float]
    failures: int
    failovers: int
    cost_usd: float
    per_provider: dict[str, int]
    breaker_trips: int
    skipped_by_breaker: int

    @property
    def served(self) -> int:
        return len(self.latencies)

    @property
    def total(self) -> int:
        return self.served + self.failures

    def percentile(self, p: float) -> float:
        if not self.latencies:
            return float("nan")
        ordered = sorted(self.latencies)
        index = min(len(ordered) - 1, int(p * len(ordered)))
        return ordered[index]

    @property
    def success_rate(self) -> float:
        return self.served / self.total if self.total else 0.0


def build_providers(requests: int, seed: int, prices: PriceBook) -> list[SyntheticProvider]:
    """The scenario. The outage covers the middle fifth of the trace."""
    outage = (int(requests * 0.40), int(requests * 0.60))
    return [
        SyntheticProvider(Profile.fast_but_flaky(outage=outage), seed=seed, prices=prices),
        SyntheticProvider(Profile.slow_but_solid(), seed=seed, prices=prices),
        SyntheticProvider(Profile.cheap_with_a_bad_tail(), seed=seed, prices=prices),
    ]


def build_trace(requests: int) -> list[CompletionRequest]:
    """Position is encoded in the request id -- that is what makes the
    provider behaviour reproducible across policies."""
    return [
        CompletionRequest(
            messages=[ChatMessage("user", f"summarise document {i}")],
            max_tokens=256,
            request_id=f"t{i:05d}",
        )
        for i in range(requests)
    ]


async def run_policy(
    policy, trace: list[CompletionRequest], seed: int, concurrency: int
) -> Outcome:
    prices = PriceBook()
    gateway = Gateway(
        build_providers(len(trace), seed, prices),
        policy,
        prices=prices,
        config=GatewayConfig(attempt_timeout=5.0, total_budget=12.0, max_attempts=3),
        breaker_config=BreakerConfig(
            failure_threshold=4, cooldown_seconds=0.0, probe_successes=1
        ),
    )

    latencies: list[float] = []
    failures = 0
    failovers = 0
    cost = 0.0
    per_provider: dict[str, int] = {}
    skipped = 0
    semaphore = asyncio.Semaphore(concurrency)

    async def one(request: CompletionRequest) -> None:
        nonlocal failures, failovers, cost, skipped
        async with semaphore:
            try:
                result = await gateway.complete(request)
            except GatewayError as error:
                failures += 1
                skipped += sum(
                    1 for a in error.result.attempts if a.skipped and "breaker" in a.skipped
                )
                return
            latencies.append(result.latency_ms)
            failovers += result.failovers
            cost += result.cost_usd
            name = result.provider or "?"
            per_provider[name] = per_provider.get(name, 0) + 1
            skipped += sum(
                1 for a in result.attempts if a.skipped and "breaker" in a.skipped
            )

    if concurrency <= 1:
        for request in trace:
            await one(request)
    else:
        await asyncio.gather(*(one(request) for request in trace))

    trips = sum(c.breaker.trips for c in gateway.candidates)
    return Outcome(
        policy=policy.name,
        latencies=latencies,
        failures=failures,
        failovers=failovers,
        cost_usd=cost,
        per_provider=per_provider,
        breaker_trips=trips,
        skipped_by_breaker=skipped,
    )


async def main_async(args: argparse.Namespace) -> int:
    trace = build_trace(args.requests)
    prices = PriceBook()
    policies = [
        StaticPriority(),
        RoundRobin(),
        RandomChoice(seed=args.seed),
        Adaptive(
            config=AdaptiveConfig(
                tail_weight=args.tail_weight,
                error_penalty=args.error_penalty,
                ms_per_dollar=args.ms_per_dollar,
                epsilon=args.epsilon,
            ),
            prices=prices,
            seed=args.seed,
        ),
    ]

    print(
        f"trace: {args.requests} requests, concurrency {args.concurrency}, "
        f"3 providers, outage on sim-fast over positions "
        f"{int(args.requests * 0.4)}-{int(args.requests * 0.6)}"
    )
    print()
    header = (
        f"{'policy':<13} {'p50':>9} {'p95':>9} {'p99':>9} "
        f"{'success':>8} {'failovers':>10} {'cost':>9} {'trips':>6}"
    )
    print(header)
    print("-" * len(header))

    outcomes: list[Outcome] = []
    for policy in policies:
        outcome = await run_policy(policy, trace, args.seed, args.concurrency)
        outcomes.append(outcome)
        print(
            f"{outcome.policy:<13} "
            f"{outcome.percentile(0.50):>8.0f}ms "
            f"{outcome.percentile(0.95):>8.0f}ms "
            f"{outcome.percentile(0.99):>8.0f}ms "
            f"{outcome.success_rate * 100:>7.2f}% "
            f"{outcome.failovers:>10} "
            f"${outcome.cost_usd:>8.3f} "
            f"{outcome.breaker_trips:>6}"
        )

    baseline = outcomes[0]
    adaptive = outcomes[-1]
    print()
    print("where each policy sent the traffic:")
    for outcome in outcomes:
        shares = ", ".join(
            f"{name} {count * 100 / max(outcome.served, 1):.0f}%"
            for name, count in sorted(outcome.per_provider.items())
        )
        print(f"  {outcome.policy:<13} {shares}")

    print()
    print(f"adaptive vs {baseline.policy} (the list everyone ships):")
    for label, p in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99)):
        before, after = baseline.percentile(p), adaptive.percentile(p)
        print(
            f"  {label:<4} {before:8.0f}ms -> {after:8.0f}ms  "
            f"({(after - before) / before * 100:+.1f}%)"
        )
    print(
        f"  fail {baseline.failures:8d}   -> {adaptive.failures:8d}     "
        f"({baseline.success_rate * 100:.2f}% -> {adaptive.success_rate * 100:.2f}% served)"
    )
    print(
        f"  cost ${baseline.cost_usd:7.3f}   -> ${adaptive.cost_usd:7.3f}     "
        f"({(adaptive.cost_usd - baseline.cost_usd) / max(baseline.cost_usd, 1e-9) * 100:+.1f}%)"
    )

    if args.sweep:
        print()
        print("tail_weight sweep -- what tail protection costs at the median:")
        print(f"  {'weight':>6} {'p50':>9} {'p95':>9} {'p99':>9} {'cost':>9} {'failovers':>10}")
        for weight in (0.0, 0.25, 0.5, 0.7, 0.85, 1.0):
            swept = await run_policy(
                Adaptive(
                    config=AdaptiveConfig(
                        tail_weight=weight,
                        error_penalty=args.error_penalty,
                        ms_per_dollar=args.ms_per_dollar,
                        epsilon=args.epsilon,
                    ),
                    prices=PriceBook(),
                    seed=args.seed,
                ),
                trace,
                args.seed,
                args.concurrency,
            )
            print(
                f"  {weight:>6.2f} {swept.percentile(0.50):>8.0f}ms "
                f"{swept.percentile(0.95):>8.0f}ms {swept.percentile(0.99):>8.0f}ms "
                f"${swept.cost_usd:>8.3f} {swept.failovers:>10}"
            )

    if args.markdown:
        print()
        print("| policy | p50 | p95 | p99 | served | fail-overs | cost | breaker trips |")
        print("|---|---|---|---|---|---|---|---|")
        for outcome in outcomes:
            print(
                f"| {outcome.policy} | {outcome.percentile(0.50):.0f} ms "
                f"| {outcome.percentile(0.95):.0f} ms | {outcome.percentile(0.99):.0f} ms "
                f"| {outcome.success_rate * 100:.2f}% | {outcome.failovers} "
                f"| ${outcome.cost_usd:.3f} | {outcome.breaker_trips} |"
            )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--tail-weight", type=float, default=AdaptiveConfig.tail_weight)
    parser.add_argument("--error-penalty", type=float, default=3.0)
    parser.add_argument("--ms-per-dollar", type=float, default=1_000_000.0)
    parser.add_argument("--epsilon", type=float, default=0.05)
    parser.add_argument("--sweep", action="store_true", help="sweep tail_weight")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
