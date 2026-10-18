from __future__ import annotations

import random

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from relay.stats import Ewma, P2Quantile, ProviderHealth


def _latencies(seed: int, n: int) -> list[float]:
    """A realistic latency sample: lognormal body with a slow tail."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        if rng.random() < 0.04:  # the tail that a p95 exists to catch
            out.append(rng.uniform(2_000, 9_000))
        else:
            out.append(rng.lognormvariate(6.2, 0.45))
    return out


def test_warmup_is_a_nearest_rank_answer() -> None:
    estimator = P2Quantile(0.95)
    assert estimator.value == 0.0
    for x in (10.0, 20.0, 30.0):
        estimator.add(x)
    assert estimator.value in (20.0, 30.0)
    assert estimator.count == 3


def test_five_observations_become_the_markers() -> None:
    estimator = P2Quantile(0.5)
    for x in (5.0, 1.0, 3.0, 2.0, 4.0):
        estimator.add(x)
    assert estimator.value == 3.0  # the median marker


@pytest.mark.parametrize("quantile", [0.5, 0.9, 0.95, 0.99])
def test_tracks_the_exact_percentile_within_tolerance(quantile: float) -> None:
    samples = _latencies(7, 20_000)
    estimator = P2Quantile(quantile)
    for x in samples:
        estimator.add(x)

    exact = float(np.percentile(samples, quantile * 100))
    error = abs(estimator.value - exact) / exact
    assert error < 0.05, f"p{quantile * 100:.0f}: {estimator.value:.1f} vs {exact:.1f}"


def test_constant_memory_regardless_of_volume() -> None:
    """The point of the algorithm: five markers, not five million samples."""
    estimator = P2Quantile(0.95)
    for x in _latencies(3, 50_000):
        estimator.add(x)
    assert len(estimator._q) == 5  # noqa: SLF001 - asserting the whole premise
    assert estimator.count == 50_000


def test_monotone_input_is_the_hard_case() -> None:
    """A steadily rising stream is where a marker estimator lags worst."""
    estimator = P2Quantile(0.95)
    for x in range(1, 10_001):
        estimator.add(float(x))
    # exact p95 of 1..10000 is 9500.05; allow the estimator its lag
    assert 9_000 < estimator.value < 9_900


def test_rejects_impossible_quantiles() -> None:
    for bad in (0.0, 1.0, -0.2, 7.0):
        with pytest.raises(ValueError):
            P2Quantile(bad)


@settings(max_examples=30, deadline=None)
@given(
    data=st.lists(
        st.floats(min_value=1.0, max_value=10_000.0, allow_nan=False),
        min_size=200,
        max_size=600,
    )
)
def test_estimate_always_lies_within_the_observed_range(data: list[float]) -> None:
    estimator = P2Quantile(0.95)
    for x in data:
        estimator.add(x)
    assert min(data) <= estimator.value <= max(data)


# ----------------------------------------------------------------------
def test_ewma_seeds_on_the_first_observation() -> None:
    """Starting from zero would make a fresh provider look flawless."""
    ewma = Ewma(0.2)
    ewma.add(1.0)
    assert ewma.value == 1.0


def test_ewma_moves_towards_recent_values() -> None:
    ewma = Ewma(0.5)
    ewma.add(0.0)
    for _ in range(8):
        ewma.add(1.0)
    assert ewma.value > 0.95


def test_ewma_forgets_a_bad_patch() -> None:
    ewma = Ewma(0.3)
    for _ in range(10):
        ewma.add(1.0)
    assert ewma.value > 0.9
    for _ in range(20):
        ewma.add(0.0)
    assert ewma.value < 0.01


def test_ewma_rejects_bad_alpha() -> None:
    for bad in (0.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            Ewma(bad)


# ----------------------------------------------------------------------
def test_health_tracks_cost_and_tail_separately() -> None:
    health = ProviderHealth("fast-but-spiky")
    for _ in range(95):
        health.record_success(100.0, 0.0001, 500)
    for _ in range(5):
        health.record_success(8_000.0, 0.0001, 500)

    assert health.requests == 100
    assert health.failures == 0
    assert health.mean_latency_ms == pytest.approx(495.0, rel=0.01)
    assert health.latency_p95.value > 1_000  # the tail the mean hides
    assert health.total_cost_usd == pytest.approx(0.01, rel=1e-6)
    assert health.total_tokens == 50_000


def test_failed_calls_count_towards_the_tail() -> None:
    """A provider that times out slowly is worse than one that refuses fast."""
    slow = ProviderHealth("slow-timeout")
    quick = ProviderHealth("fast-refusal")
    for _ in range(20):
        slow.record_failure(30_000.0)
        quick.record_failure(5.0)
    assert slow.latency_p95.value > quick.latency_p95.value
    assert slow.error_rate.value == pytest.approx(quick.error_rate.value)


def test_snapshot_is_json_ready() -> None:
    health = ProviderHealth("p")
    health.record_success(120.0, 0.002, 300)
    health.record_failure(900.0)
    snapshot = health.snapshot()
    assert snapshot["provider"] == "p"
    assert snapshot["requests"] == 2
    assert snapshot["failures"] == 1
    assert 0.0 < float(snapshot["error_rate_ewma"]) < 1.0
