from __future__ import annotations

from pathlib import Path

import pytest

from relay.providers.base import Completion, Usage
from relay.routing.gateway import Attempt, GatewayResult
from relay.store import RequestStore


@pytest.fixture
def store(tmp_path: Path) -> RequestStore:
    s = RequestStore(tmp_path / "relay.db")
    yield s
    s.close()


def served(
    index: int,
    *,
    provider: str = "sim-fast",
    latency_ms: float = 120.0,
    cost: float = 0.0004,
    failovers: int = 0,
) -> GatewayResult:
    return GatewayResult(
        request_id=f"t{index:05d}",
        policy="adaptive",
        attempts=(Attempt(provider, ok=True, latency_ms=latency_ms),),
        completion=Completion(
            text="ok",
            model="sim-fast",
            provider=provider,
            usage=Usage(500, 100),
            latency_ms=latency_ms,
            cost_usd=cost,
        ),
        latency_ms=latency_ms + failovers * 50,
    )


def unserved(index: int) -> GatewayResult:
    return GatewayResult(
        request_id=f"t{index:05d}",
        policy="adaptive",
        attempts=(
            Attempt("sim-fast", ok=False, latency_ms=900.0, error="503"),
            Attempt("sim-cheap", ok=False, skipped="breaker_open"),
        ),
        completion=None,
        latency_ms=900.0,
    )


def test_records_successes_and_failures(store: RequestStore) -> None:
    store.record(served(1))
    store.record(unserved(2))
    assert store.count() == 2

    rows = store.recent()
    assert {row["request_id"] for row in rows} == {"t00001", "t00002"}
    failed = next(row for row in rows if not row["ok"])
    assert "503" in str(failed["error"])
    assert failed["attempts"][1]["skipped"] == "breaker_open"


def test_attempts_round_trip_as_structured_data(store: RequestStore) -> None:
    store.record(unserved(1))
    attempts = store.recent()[0]["attempts"]
    assert isinstance(attempts, list)
    assert attempts[0]["provider"] == "sim-fast"
    assert attempts[0]["ok"] is False


def test_recent_is_newest_first_and_limited(store: RequestStore) -> None:
    for i in range(10):
        store.record(served(i))
    rows = store.recent(limit=3)
    assert len(rows) == 3
    assert rows[0]["ts"] >= rows[-1]["ts"]


def test_failures_view(store: RequestStore) -> None:
    store.record(served(1))
    store.record(unserved(2))
    store.record(unserved(3))
    assert len(store.failures()) == 2


def test_totals_include_exact_percentiles(store: RequestStore) -> None:
    for i in range(100):
        store.record(served(i, latency_ms=float(i + 1)))
    totals = store.totals()
    assert totals["requests"] == 100
    assert totals["failures"] == 0
    assert totals["p50_ms"] == pytest.approx(51.0, abs=1.0)
    assert totals["p95_ms"] == pytest.approx(96.0, abs=1.0)
    assert totals["cost_usd"] == pytest.approx(0.04, rel=1e-3)
    assert totals["tokens"] == 60_000


def test_totals_can_be_windowed(store: RequestStore) -> None:
    store.record(served(1))
    assert store.totals(since_seconds=3600)["requests"] == 1
    assert store.totals(since_seconds=0)["requests"] == 0


def test_rollup_per_provider(store: RequestStore) -> None:
    for i in range(6):
        store.record(served(i, provider="sim-fast", latency_ms=100.0))
    for i in range(6, 10):
        store.record(served(i, provider="sim-balanced", latency_ms=700.0))
    store.record(unserved(99))

    rollups = {r.provider: r for r in store.by_provider()}
    assert rollups["sim-fast"].requests == 6
    assert rollups["sim-balanced"].p50_ms == pytest.approx(700.0)
    # an unserved request has no provider, and is reported as such rather
    # than silently dropped from the rollup
    assert rollups["(unserved)"].requests == 1
    assert rollups["(unserved)"].failures == 1


def test_timeline_buckets_recent_traffic(store: RequestStore) -> None:
    for i in range(20):
        store.record(served(i))
    series = store.timeline(buckets=5, bucket_seconds=10.0)
    assert len(series) == 5
    assert sum(b["requests"] for b in series) == 20
    assert series[-1]["t"] <= 0  # buckets are relative to now, newest last


def test_trim_keeps_only_the_newest_rows(tmp_path: Path) -> None:
    store = RequestStore(tmp_path / "relay.db", keep_rows=10)
    for i in range(25):
        store.record(served(i))
    deleted = store.trim()
    assert deleted == 15
    assert store.count() == 10
    store.close()


def test_logging_failure_never_breaks_a_served_request(store: RequestStore) -> None:
    """The completion is the product; the log is bookkeeping."""
    store.close()  # a closed connection makes every write raise
    store.record(served(1))  # must not propagate


def test_survives_reopening_the_file(tmp_path: Path) -> None:
    path = tmp_path / "relay.db"
    first = RequestStore(path)
    first.record(served(1))
    first.close()

    second = RequestStore(path)
    assert second.count() == 1
    second.close()
