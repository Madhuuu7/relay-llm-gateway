/* The controlled comparison: four policies, one trace, identical providers.
 *
 * The live panels below show a gateway doing whatever it is doing right
 * now, which is suggestive and proves nothing — the traffic is different
 * every time you look. This is the part that is actually evidence.
 *
 * One trace of 4,000 requests is replayed through all four policies with
 * provider latency and failure derived from a hash of (provider,
 * position), so the behaviour of the upstreams is byte-identical across
 * the four runs and the only thing that differs is where each request was
 * sent. That is what makes the difference attributable to routing rather
 * than to luck, and it is why the number is reproducible on any machine.
 *
 * It is drawn rather than only tabulated because the claim is about a
 * shape. Each policy gets one line on a shared millisecond axis: a
 * hairline up to its median, a bar from the median to p95, and a thin run
 * on to p99. Three of those bars are long and one is short, and that is
 * the whole project. The percentage lives in the sentence underneath,
 * where a number that needs a caption belongs — as a 72-point figure at
 * the top of a page it would be asking to be believed rather than read.
 *
 * The bars are drawn identically for every policy; only the winning row is
 * tinted. Emphasising the bar itself would be drawing the conclusion into
 * the evidence.
 */

import type { Benchmark as BenchmarkData, PolicyResult } from "../lib/api";

function ms(v: number): string {
  return v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${Math.round(v)}ms`;
}

function delta(after: number, before: number): string {
  if (!before) return "—";
  const pct = ((after - before) / before) * 100;
  return `${pct > 0 ? "+" : ""}${pct.toFixed(1)}%`;
}

/** Round up to a tick a reader can hold in their head: 1s, 2s, 5s, 10s… */
function niceMax(v: number): number {
  const steps = [1000, 2000, 3000, 4000, 5000, 6000, 8000, 10000, 15000, 20000];
  return steps.find((s) => s >= v) ?? Math.ceil(v / 10000) * 10000;
}

export function Benchmark({ data }: { data: BenchmarkData }) {
  const baseline =
    data.policies.find((p) => p.policy === data.baseline) ?? data.policies[0];
  const adaptive =
    data.policies.find((p) => p.policy === "adaptive") ??
    data.policies[data.policies.length - 1];
  const worst = Math.max(...data.policies.map((p) => p.p95_ms), 1);
  const full = niceMax(Math.max(...data.policies.map((p) => p.p99_ms), 1));
  const at = (v: number) => `${Math.min(100, (v / full) * 100)}%`;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * full);

  const better = (p: PolicyResult) => p.p95_ms <= adaptive.p95_ms;

  return (
    <>
      <div className="tails">
        {data.policies.map((p) => (
          <div
            key={p.policy}
            className={`tailrow${p.policy === adaptive.policy ? " win" : ""}`}
          >
            <span className="pol">{p.policy}</span>
            <div
              className="axis"
              role="img"
              aria-label={`${p.policy}: median ${ms(p.p50_ms)}, p95 ${ms(
                p.p95_ms,
              )}, p99 ${ms(p.p99_ms)}`}
            >
              <span className="lead" style={{ left: 0, width: at(p.p50_ms) }} />
              <span
                className="body"
                style={{
                  left: at(p.p50_ms),
                  width: `${((p.p95_ms - p.p50_ms) / full) * 100}%`,
                }}
              />
              <span
                className="whisker"
                style={{
                  left: at(p.p95_ms),
                  width: `${
                    (Math.max(0, p.p99_ms - p.p95_ms) / full) * 100
                  }%`,
                }}
              />
              <span className="at50" style={{ left: at(p.p50_ms) }} />
              <span className="cap" style={{ left: at(p.p99_ms) }} />
            </div>
          </div>
        ))}

        <div className="tailscale" aria-hidden="true">
          {ticks.map((t) => (
            <span key={t}>{t === 0 ? "0" : ms(t)}</span>
          ))}
        </div>

        <p className="tailkey">
          Each line runs from zero to the slowest one request in a hundred.
          The bar is the stretch between the median and p95 — the part a
          caller actually feels — and the thin run past it ends at p99.
        </p>
      </div>

      <p className="readout">
        Adaptive routing holds its median and gives up the tail: p95{" "}
        <b>{ms(baseline.p95_ms)}</b> down to <b>{ms(adaptive.p95_ms)}</b>,{" "}
        {delta(adaptive.p95_ms, baseline.p95_ms)} against the static fallback
        list most gateways ship, with{" "}
        {delta(adaptive.breaker_trips, baseline.breaker_trips)} circuit
        breaker trips over {data.requests.toLocaleString()} replayed requests
        at concurrency {data.concurrency}.
      </p>

      <div className="scroll" style={{ marginTop: 22 }}>
        <table>
          <thead>
            <tr>
              <th>Policy</th>
              <th className="num">p50</th>
              <th className="num">p95</th>
              <th className="num">p99</th>
              <th className="num">Fail-overs</th>
              <th className="num">Breaker trips</th>
              <th className="num">Cost</th>
            </tr>
          </thead>
          <tbody>
            {data.policies.map((p) => (
              <tr key={p.policy} className={better(p) ? "best" : undefined}>
                <td>
                  <div className="p95bar">
                    <span
                      style={{ width: `${(p.p95_ms / worst) * 100}%` }}
                      className={p.policy === "adaptive" ? "win" : undefined}
                    />
                  </div>
                  {p.policy}
                </td>
                <td className="num">{ms(p.p50_ms)}</td>
                <td className="num">{ms(p.p95_ms)}</td>
                <td className="num">{ms(p.p99_ms)}</td>
                <td className="num">{p.failovers}</td>
                <td className="num">{p.breaker_trips}</td>
                <td className="num">${p.cost_usd.toFixed(3)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="sub" style={{ marginTop: 14 }}>
        Adaptive is not cheaper on every axis and the table says so: p99 is
        slightly worse, because the method that keeps the tail short is the
        same one that occasionally probes a provider it is unsure about.
        Reproduce the whole thing with{" "}
        <code>python scripts/replay.py --requests 4000 --concurrency 16</code>.
      </p>
    </>
  );
}
