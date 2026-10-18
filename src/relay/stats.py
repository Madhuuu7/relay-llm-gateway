"""Streaming statistics the router decides on.

A gateway has to answer "which provider is fast right now?" on every request,
for every provider, forever. Two ways to get a p95 out of a live stream:

* Keep the samples. A sliding window of the last N latencies, sorted on
  demand. Exact over the window, but it is O(N) memory per provider and the
  window length silently defines how long a bad minute keeps hurting.
* Keep five numbers. The **P-squared algorithm** (Jain & Chlamtac, 1985)
  tracks a quantile by maintaining five markers and nudging them with a
  parabolic prediction as observations arrive. Constant memory, constant time,
  no stored samples.

P-squared is implemented here because a gateway is exactly its use case, and
because "I can estimate a p95 in five floats" is a more interesting answer
than "I sorted a list". It is an *estimator*: on adversarial or strongly
drifting input it lags, so the tests pin it against exact percentiles on
realistic latency distributions rather than asserting equality.

Error rates use an EWMA instead. A rate is a mean, and a mean needs no
markers -- but it does need to forget, which is what the decay gives it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class P2Quantile:
    """Single-quantile estimator in constant space (Jain & Chlamtac 1985).

    ``P2Quantile(0.95).value`` tracks the 95th percentile of everything it has
    seen using five markers: the running minimum, three interior estimates,
    and the running maximum. Each new observation moves the marker cells by
    one and the interior markers are re-fitted to a parabola through their
    neighbours, falling back to a linear step when the parabola would put a
    marker out of order.
    """

    __slots__ = ("p", "_count", "_boot", "_q", "_n", "_np", "_dn")

    def __init__(self, p: float = 0.95) -> None:
        if not 0.0 < p < 1.0:
            raise ValueError(f"quantile must be in (0, 1), got {p}")
        self.p = float(p)
        self._count = 0
        self._boot: list[float] = []
        self._q: list[float] = []
        self._n = [1.0, 2.0, 3.0, 4.0, 5.0]
        self._np = [1.0, 1.0 + 2.0 * p, 1.0 + 4.0 * p, 3.0 + 2.0 * p, 5.0]
        self._dn = [0.0, p / 2.0, p, (1.0 + p) / 2.0, 1.0]

    @property
    def count(self) -> int:
        return self._count

    def add(self, x: float) -> None:
        x = float(x)
        self._count += 1

        # The first five observations are the initial markers; until then
        # there is nothing to interpolate between.
        if len(self._q) < 5:
            self._boot.append(x)
            if len(self._boot) == 5:
                self._q = sorted(self._boot)
            return

        q, n, np_, dn = self._q, self._n, self._np, self._dn

        if x < q[0]:
            q[0] = x
            k = 0
        elif x < q[1]:
            k = 0
        elif x < q[2]:
            k = 1
        elif x < q[3]:
            k = 2
        elif x <= q[4]:
            k = 3
        else:
            q[4] = x
            k = 3

        for i in range(k + 1, 5):
            n[i] += 1.0
        for i in range(5):
            np_[i] += dn[i]

        for i in range(1, 4):
            gap = np_[i] - n[i]
            left = n[i] - n[i - 1]
            right = n[i + 1] - n[i]
            if (gap >= 1.0 and right > 1.0) or (gap <= -1.0 and left > 1.0):
                step = 1.0 if gap > 0 else -1.0
                # parabolic prediction through (i-1, i, i+1)
                candidate = q[i] + step / (n[i + 1] - n[i - 1]) * (
                    (left + step) * (q[i + 1] - q[i]) / right
                    + (right - step) * (q[i] - q[i - 1]) / left
                )
                if q[i - 1] < candidate < q[i + 1]:
                    q[i] = candidate
                else:  # parabola would break the ordering; take a linear step
                    j = i + int(step)
                    q[i] = q[i] + step * (q[j] - q[i]) / (n[j] - n[i])
                n[i] += step

    @property
    def value(self) -> float:
        """Current estimate; a nearest-rank answer while still warming up."""
        if not self._q:
            if not self._boot:
                return 0.0
            ordered = sorted(self._boot)
            index = min(len(ordered) - 1, int(self.p * len(ordered)))
            return ordered[index]
        return self._q[2]


class Ewma:
    """Exponentially weighted mean that forgets at a fixed rate.

    ``alpha`` is the weight of the newest observation. The first observation
    seeds the mean outright: starting from zero would make a provider look
    perfect for its first few dozen requests, which is precisely when the
    router knows least about it.
    """

    __slots__ = ("alpha", "_value", "_count")

    def __init__(self, alpha: float = 0.1) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError(f"alpha must be in (0, 1], got {alpha}")
        self.alpha = float(alpha)
        self._value = 0.0
        self._count = 0

    def add(self, x: float) -> None:
        x = float(x)
        self._count += 1
        if self._count == 1:
            self._value = x
        else:
            self._value += self.alpha * (x - self._value)

    @property
    def value(self) -> float:
        return self._value

    @property
    def count(self) -> int:
        return self._count


@dataclass
class ProviderHealth:
    """What the router knows about one provider, updated on every outcome.

    Latency is tracked as a p95 rather than a mean because a gateway's job is
    to protect the tail: a provider whose mean is 400 ms and whose p95 is 9 s
    is a worse choice than one flat at 900 ms, and the mean hides that.
    """

    name: str
    latency_p95: P2Quantile = field(default_factory=lambda: P2Quantile(0.95))
    latency_p50: P2Quantile = field(default_factory=lambda: P2Quantile(0.50))
    error_rate: Ewma = field(default_factory=lambda: Ewma(0.15))
    requests: int = 0
    failures: int = 0
    total_latency_ms: float = 0.0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    in_flight: int = 0

    def record_success(self, latency_ms: float, cost_usd: float, tokens: int) -> None:
        self.requests += 1
        self.latency_p95.add(latency_ms)
        self.latency_p50.add(latency_ms)
        self.error_rate.add(0.0)
        self.total_latency_ms += latency_ms
        self.total_cost_usd += cost_usd
        self.total_tokens += tokens

    def record_failure(self, latency_ms: float) -> None:
        self.requests += 1
        self.failures += 1
        self.error_rate.add(1.0)
        # A failed call still consumed wall-clock time, and a provider that
        # times out slowly is worse than one that refuses quickly -- so the
        # latency it burned counts towards its tail.
        self.latency_p95.add(latency_ms)
        self.latency_p50.add(latency_ms)
        self.total_latency_ms += latency_ms

    @property
    def mean_latency_ms(self) -> float:
        return self.total_latency_ms / self.requests if self.requests else 0.0

    @property
    def observed_error_rate(self) -> float:
        return self.failures / self.requests if self.requests else 0.0

    def snapshot(self) -> dict[str, float | int | str]:
        return {
            "provider": self.name,
            "requests": self.requests,
            "failures": self.failures,
            "p50_ms": round(self.latency_p50.value, 1),
            "p95_ms": round(self.latency_p95.value, 1),
            "mean_ms": round(self.mean_latency_ms, 1),
            "error_rate_ewma": round(self.error_rate.value, 4),
            "error_rate_observed": round(self.observed_error_rate, 4),
            "cost_usd": round(self.total_cost_usd, 6),
            "tokens": self.total_tokens,
            "in_flight": self.in_flight,
        }
