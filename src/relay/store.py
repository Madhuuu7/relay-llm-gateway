"""Request log and rollups, in SQLite.

Why a database at all: the in-memory health trackers answer "what is happening
now", which is what routing needs, but they cannot answer "what did this cost
last week", "which provider served that failed request", or "did the p95 get
better after we changed the policy". Those are the questions an operator
actually asks, and they need rows.

Why SQLite: the gateway is one process, the write rate is one row per request,
and the queries are small aggregates. A Postgres dependency would add a
network hop and a connection pool to the request path for no gain at this
size. The cost is honest and documented -- on a free-tier host with an
ephemeral disk the file does not survive a restart, and a second instance
would have its own copy, so this is a single-node log, not a warehouse.

Writes happen on the event loop. One ``INSERT`` into a WAL-mode SQLite
database is tens of microseconds, which is four orders of magnitude below the
provider latency it is recording; pushing it to a thread would add more
synchronisation than it saves. The write is wrapped so that a logging failure
can never fail a request that the provider already served -- the completion is
the product, the log is bookkeeping.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .routing.gateway import GatewayResult

log = logging.getLogger("relay.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id    TEXT PRIMARY KEY,
    ts            REAL NOT NULL,
    policy        TEXT NOT NULL,
    ok            INTEGER NOT NULL,
    provider      TEXT,
    model         TEXT,
    latency_ms    REAL NOT NULL,
    cost_usd      REAL NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    failovers     INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    attempts      TEXT NOT NULL DEFAULT '[]'
);
-- Every dashboard query is "recent rows, newest first", and every rollup
-- filters on ts; without this index both become a full scan as the log grows.
CREATE INDEX IF NOT EXISTS requests_ts ON requests (ts DESC);
CREATE INDEX IF NOT EXISTS requests_provider_ts ON requests (provider, ts DESC);
"""


@dataclass(frozen=True)
class ProviderRollup:
    provider: str
    requests: int
    failures: int
    p50_ms: float
    p95_ms: float
    cost_usd: float
    tokens: int


class RequestStore:
    """Append-only request log with a few aggregates over it."""

    def __init__(self, path: str | Path = "relay.db", *, keep_rows: int = 50_000) -> None:
        self.path = str(path)
        self.keep_rows = keep_rows
        self._connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        # WAL so a long-running read (the dashboard) never blocks a write (a
        # request finishing).
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.executescript(SCHEMA)
        self._writes = 0

    # ------------------------------------------------------------------
    def record(self, result: GatewayResult) -> None:
        """Log one gateway result. Never raises."""
        try:
            self._insert(result)
        except Exception:  # noqa: BLE001 - bookkeeping must not fail a served request
            log.exception("failed to log %s", result.request_id)

    def _insert(self, result: GatewayResult) -> None:
        completion = result.completion
        error = None
        if completion is None:
            error = "; ".join(
                f"{a.provider}: {a.error or a.skipped}" for a in result.attempts
            ) or "no provider was eligible"

        self._connection.execute(
            """
            INSERT OR REPLACE INTO requests (
                request_id, ts, policy, ok, provider, model, latency_ms,
                cost_usd, input_tokens, output_tokens, failovers, error, attempts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.request_id,
                time.time(),
                result.policy,
                1 if result.ok else 0,
                result.provider,
                completion.model if completion else None,
                result.latency_ms,
                result.cost_usd,
                completion.usage.input_tokens if completion else 0,
                completion.usage.output_tokens if completion else 0,
                result.failovers,
                error,
                json.dumps([
                    {
                        "provider": a.provider,
                        "ok": a.ok,
                        "latency_ms": round(a.latency_ms, 1),
                        "error": a.error,
                        "skipped": a.skipped,
                    }
                    for a in result.attempts
                ]),
            ),
        )
        self._writes += 1
        # Trim occasionally rather than every write: the check is a COUNT, and
        # paying it 50,000 times to delete once is the wrong trade.
        if self._writes % 1_000 == 0:
            self.trim()

    def trim(self) -> int:
        """Drop the oldest rows beyond ``keep_rows``. Returns rows deleted."""
        cursor = self._connection.execute(
            """
            DELETE FROM requests WHERE request_id IN (
                SELECT request_id FROM requests ORDER BY ts DESC LIMIT -1 OFFSET ?
            )
            """,
            (self.keep_rows,),
        )
        return cursor.rowcount or 0

    # ------------------------------------------------------------------
    def count(self) -> int:
        return int(self._connection.execute("SELECT count(*) FROM requests").fetchone()[0])

    def recent(self, limit: int = 50) -> list[dict[str, object]]:
        rows = self._connection.execute(
            """
            SELECT request_id, ts, policy, ok, provider, model, latency_ms,
                   cost_usd, input_tokens, output_tokens, failovers, error, attempts
            FROM requests ORDER BY ts DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def failures(self, limit: int = 50) -> list[dict[str, object]]:
        rows = self._connection.execute(
            "SELECT * FROM requests WHERE ok = 0 ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def totals(self, *, since_seconds: float | None = None) -> dict[str, object]:
        where, params = self._window(since_seconds)
        row = self._connection.execute(
            f"""
            SELECT count(*) AS requests,
                   sum(1 - ok) AS failures,
                   coalesce(sum(cost_usd), 0) AS cost_usd,
                   coalesce(sum(input_tokens + output_tokens), 0) AS tokens,
                   coalesce(sum(failovers), 0) AS failovers,
                   coalesce(avg(latency_ms), 0) AS mean_ms
            FROM requests {where}
            """,
            params,
        ).fetchone()
        latencies = self._latencies(where, params)
        return {
            "requests": int(row["requests"] or 0),
            "failures": int(row["failures"] or 0),
            "cost_usd": round(float(row["cost_usd"] or 0.0), 6),
            "tokens": int(row["tokens"] or 0),
            "failovers": int(row["failovers"] or 0),
            "mean_ms": round(float(row["mean_ms"] or 0.0), 1),
            "p50_ms": _percentile(latencies, 0.50),
            "p95_ms": _percentile(latencies, 0.95),
            "p99_ms": _percentile(latencies, 0.99),
        }

    def by_provider(self, *, since_seconds: float | None = None) -> list[ProviderRollup]:
        where, params = self._window(since_seconds)
        rows = self._connection.execute(
            f"""
            SELECT provider,
                   count(*) AS requests,
                   sum(1 - ok) AS failures,
                   coalesce(sum(cost_usd), 0) AS cost_usd,
                   coalesce(sum(input_tokens + output_tokens), 0) AS tokens
            FROM requests {where}
            GROUP BY provider ORDER BY requests DESC
            """,
            params,
        ).fetchall()

        out: list[ProviderRollup] = []
        for row in rows:
            name = row["provider"] or "(unserved)"
            clause = f"{where} AND provider IS ?" if where else "WHERE provider IS ?"
            latencies = self._latencies(clause, (*params, row["provider"]))
            out.append(
                ProviderRollup(
                    provider=name,
                    requests=int(row["requests"]),
                    failures=int(row["failures"] or 0),
                    p50_ms=_percentile(latencies, 0.50),
                    p95_ms=_percentile(latencies, 0.95),
                    cost_usd=round(float(row["cost_usd"] or 0.0), 6),
                    tokens=int(row["tokens"] or 0),
                )
            )
        return out

    def timeline(self, *, buckets: int = 30, bucket_seconds: float = 10.0) -> list[dict[str, object]]:
        """Recent traffic bucketed by time, for the dashboard's chart."""
        now = time.time()
        start = now - buckets * bucket_seconds
        rows = self._connection.execute(
            """
            SELECT ts, ok, latency_ms, cost_usd FROM requests
            WHERE ts >= ? ORDER BY ts
            """,
            (start,),
        ).fetchall()

        series: list[dict[str, object]] = []
        for index in range(buckets):
            lo = start + index * bucket_seconds
            hi = lo + bucket_seconds
            window = [r for r in rows if lo <= r["ts"] < hi]
            latencies = sorted(float(r["latency_ms"]) for r in window)
            series.append(
                {
                    "t": round(lo - now, 1),
                    "requests": len(window),
                    "failures": sum(1 for r in window if not r["ok"]),
                    "p95_ms": _percentile(latencies, 0.95),
                    "cost_usd": round(sum(float(r["cost_usd"]) for r in window), 6),
                }
            )
        return series

    def close(self) -> None:
        self._connection.close()

    # ------------------------------------------------------------------
    @staticmethod
    def _window(since_seconds: float | None) -> tuple[str, tuple]:
        if since_seconds is None:
            return "", ()
        return "WHERE ts >= ?", (time.time() - since_seconds,)

    def _latencies(self, where: str, params: Iterable) -> list[float]:
        rows = self._connection.execute(
            f"SELECT latency_ms FROM requests {where}", tuple(params)
        ).fetchall()
        return sorted(float(row["latency_ms"]) for row in rows)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, object]:
        data = {key: row[key] for key in row.keys()}
        data["ok"] = bool(data["ok"])
        if isinstance(data.get("attempts"), str):
            try:
                data["attempts"] = json.loads(data["attempts"])
            except json.JSONDecodeError:
                data["attempts"] = []
        return data


def _percentile(ordered: list[float], p: float) -> float:
    """Exact percentile over stored rows.

    Exact here, estimated in :mod:`relay.stats`: this runs over a bounded
    result set a few times a second for a human to look at, while the router's
    p95 runs on every request for every provider. Different constraints,
    different algorithm.
    """
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(p * len(ordered)))
    return round(ordered[index], 1)
