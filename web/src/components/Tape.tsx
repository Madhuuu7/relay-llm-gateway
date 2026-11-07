/* The tape: one tick per request, coloured by who served it.
 *
 * Aggregates hide the thing worth seeing. A p95 chart tells you a provider
 * got slower; it does not show the moment the breaker trips and the traffic
 * moves. Laid out one tick per request, in order, that moment is a visible
 * change of colour partway along the strip — which is the clearest statement
 * of what adaptive routing does that this project can make.
 *
 * Height encodes latency so a slow provider is also a tall tick, and a
 * failure is red regardless of who it was, because a failed request belongs
 * to the gateway rather than to a provider.
 */

import { providerIndex, type Ranked } from "../lib/score";
import type { RequestRow } from "../lib/api";

export function Tape({
  rows,
  ranking,
}: {
  rows: RequestRow[];
  ranking: Ranked[];
}) {
  if (!rows.length) {
    return (
      <p className="state">
        No requests yet. Send some traffic and each one appears here as a
        tick, in order, coloured by the provider that served it.
      </p>
    );
  }

  const names = ranking.map((r) => r.provider);
  // Oldest on the left: the point of the strip is to read left to right as
  // time, and the API returns newest first.
  const ordered = [...rows].reverse();
  const slowest = Math.max(...ordered.map((r) => r.latency_ms ?? 0), 1);

  return (
    <>
      <div className="tape">
        {ordered.map((row) => {
          const colour = row.provider
            ? `var(--p${providerIndex(names, row.provider)})`
            : "var(--down)";
          // Square-rooted so one 9-second outlier does not flatten every
          // other tick to a line. The axis below says the scale is relative.
          const height = Math.max(
            4,
            Math.round(Math.sqrt((row.latency_ms ?? 0) / slowest) * 46),
          );
          const title = row.ok
            ? `${row.provider} · ${Math.round(row.latency_ms ?? 0)}ms${
                row.failovers ? ` · ${row.failovers} fail-over` : ""
              }`
            : `failed: ${row.error ?? "unknown"}`;
          return (
            <div
              key={row.request_id}
              className={`tick${row.ok ? "" : " failed"}`}
              title={title}
            >
              <span
                className="body"
                style={{ height, background: colour }}
              />
            </div>
          );
        })}
      </div>
      <div className="tape-axis">
        <span>older</span>
        <span>
          height is latency, relative to {Math.round(slowest)}ms
        </span>
        <span>newest</span>
      </div>
    </>
  );
}
