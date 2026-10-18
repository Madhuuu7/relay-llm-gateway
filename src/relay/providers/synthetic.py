"""A provider that fakes latency and failure, deterministically.

This is what makes the routing claim measurable without spending money. The
question "does adaptive routing beat round-robin?" needs two runs over the
*same* traffic against the *same* provider behaviour, differing only in the
routing decision. Live providers cannot give that: their latency is whatever
the internet was doing that minute, so a comparison between two policies run
back to back measures the weather.

Determinism without ordering bias
---------------------------------
The obvious implementation -- one RNG per provider, drawn on each call -- is
subtly useless here. If policy A sends request 5 to provider X and policy B
sends it to Y, the two runs consume the RNG streams in different orders and
every later outcome diverges. The comparison then includes noise the policies
did not cause.

So an outcome is a **pure function of (seed, provider, trace position)**,
derived by hashing. Whoever asks provider X about request 42 gets the same
latency and the same success or failure, in any order, in any run. The only
difference between two policies replaying one trace is where they sent each
request -- which is exactly the thing being measured.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Sequence

from ..pricing import PriceBook
from .base import (
    Completion,
    CompletionRequest,
    ProviderTimeout,
    Unavailable,
    Usage,
)

_TRAILING_DIGITS = re.compile(r"(\d+)$")


async def _no_sleep(_seconds: float) -> None:
    """Simulated time passes for free, so a 10k-request replay is instant."""
    return None


@dataclass(frozen=True)
class Profile:
    """How one simulated provider behaves.

    The three defaults below are drawn to make the routing problem real: a
    provider can be fast but flaky, slow but dependable, or cheap with a bad
    tail, and no static priority list is right for all three at once.
    """

    name: str
    model: str
    base_latency_ms: float = 400.0
    spread: float = 0.35
    """Lognormal sigma: latency spread around the base, in log space."""

    tail_probability: float = 0.02
    tail_latency_ms: float = 6_000.0
    error_rate: float = 0.01
    outage_spans: tuple[tuple[int, int], ...] = ()
    """Trace positions [start, end) during which every call fails -- a
    provider having an incident in the middle of the run."""

    output_tokens: int = 220
    input_tokens: int = 700

    @staticmethod
    def fast_but_flaky(outage: tuple[int, int] | None = None) -> "Profile":
        return Profile(
            name="sim-fast",
            model="sim-fast",
            base_latency_ms=240.0,
            spread=0.3,
            tail_probability=0.03,
            tail_latency_ms=4_000.0,
            error_rate=0.06,
            outage_spans=(outage,) if outage else (),
        )

    @staticmethod
    def slow_but_solid() -> "Profile":
        return Profile(
            name="sim-balanced",
            model="sim-balanced",
            base_latency_ms=750.0,
            spread=0.2,
            tail_probability=0.01,
            tail_latency_ms=2_500.0,
            error_rate=0.004,
        )

    @staticmethod
    def cheap_with_a_bad_tail() -> "Profile":
        return Profile(
            name="sim-cheap",
            model="sim-cheap",
            base_latency_ms=500.0,
            spread=0.55,
            tail_probability=0.09,
            tail_latency_ms=9_000.0,
            error_rate=0.02,
        )


@dataclass
class SyntheticProvider:
    """A :class:`~relay.providers.base.Provider` backed by arithmetic."""

    profile: Profile
    seed: int = 0
    sleep: Callable[[float], Awaitable[None]] = _no_sleep
    prices: PriceBook = field(default_factory=PriceBook)
    calls: int = 0
    _counter: int = 0

    @property
    def name(self) -> str:
        return self.profile.name

    @property
    def models(self) -> Sequence[str]:
        return (self.profile.model,)

    async def complete(
        self, request: CompletionRequest, *, timeout: float
    ) -> Completion:
        self.calls += 1
        position = self._position(request)
        latency_ms, failed = self.outcome(position)

        # A timeout burns the whole budget and *then* fails, which is the
        # expensive failure mode a gateway has to price in.
        if latency_ms > timeout * 1000.0:
            await self.sleep(timeout)
            raise ProviderTimeout(
                f"no response in {timeout:.2f}s",
                provider=self.name,
                elapsed_ms=timeout * 1000.0,
            )

        await self.sleep(latency_ms / 1000.0)
        if failed:
            raise Unavailable(
                "simulated upstream failure",
                provider=self.name,
                status=503,
                elapsed_ms=latency_ms,
            )

        usage = Usage(self.profile.input_tokens, self.profile.output_tokens)
        return Completion(
            text=f"[{self.name}] simulated completion for {request.request_id}",
            model=self.profile.model,
            provider=self.name,
            usage=usage,
            latency_ms=latency_ms,
            cost_usd=self.prices.cost(self.profile.model, usage),
        )

    async def aclose(self) -> None:
        return None

    # ------------------------------------------------------------------
    def outcome(self, position: int) -> tuple[float, bool]:
        """``(latency_ms, failed)`` for this provider at this trace position.

        Pure: same inputs, same answer, forever. That is the property the
        policy comparison rests on.
        """
        profile = self.profile
        for start, end in profile.outage_spans:
            if start <= position < end:
                # An outage still costs time: the request hangs, then fails.
                return profile.tail_latency_ms, True

        u_latency = self._uniform(position, "latency")
        u_tail = self._uniform(position, "tail")
        u_error = self._uniform(position, "error")

        if u_tail < profile.tail_probability:
            latency = profile.tail_latency_ms * (0.75 + 0.5 * u_latency)
        else:
            # Lognormal: latency distributions are right-skewed, and a normal
            # one would produce negative milliseconds at the low end.
            z = _normal_from_uniform(u_latency)
            latency = profile.base_latency_ms * math.exp(profile.spread * z)

        return latency, u_error < profile.error_rate

    def _position(self, request: CompletionRequest) -> int:
        """Trace position, read off the request id when it carries one.

        The replay harness names requests ``t00042``; anything else falls back
        to a local counter, which is fine for a live demo where comparability
        across runs is not the point.
        """
        match = _TRAILING_DIGITS.search(request.request_id)
        if match:
            return int(match.group(1))
        self._counter += 1
        return self._counter

    def _uniform(self, position: int, stream: str) -> float:
        digest = hashlib.blake2b(
            f"{self.seed}:{self.profile.name}:{stream}:{position}".encode(),
            digest_size=8,
        ).digest()
        return int.from_bytes(digest, "big") / float(1 << 64)


def _normal_from_uniform(u: float) -> float:
    """Inverse normal CDF (Acklam's rational approximation).

    Needed because the outcome has to be a *function* of the hash, and
    ``random.gauss`` consumes a stream instead.
    """
    u = min(max(u, 1e-12), 1.0 - 1e-12)
    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00)
    low, high = 0.02425, 1.0 - 0.02425

    if u < low:
        q = math.sqrt(-2.0 * math.log(u))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
        )
    if u > high:
        q = math.sqrt(-2.0 * math.log(1.0 - u))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
        )
    q = u - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    )


def real_sleep(seconds: float) -> Awaitable[None]:
    """For a live demo, where simulated latency should actually be felt."""
    return asyncio.sleep(seconds)
