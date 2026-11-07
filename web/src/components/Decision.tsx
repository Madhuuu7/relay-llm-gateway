/* The routing decision, with the score taken apart.
 *
 * Every gateway dashboard shows per-provider latency. The thing this one can
 * show that they cannot is *why the router chose what it chose*: the policy
 * publishes its score, and the score is a sum of four terms that frequently
 * disagree. The fastest provider is usually carrying the largest price term,
 * which is exactly the trade a static fallback list cannot express and the
 * reason this project exists.
 *
 * Lower is better, so the shortest bar wins — which is the one visual
 * convention here worth stating out loud, because every other bar chart a
 * reader has seen rewards length.
 */

import { decompose, providerIndex, segments, type Ranked } from "../lib/score";

const SEG_CLASS: Record<string, string> = {
  latency: "seg-latency",
  error: "seg-error",
  queue: "seg-queue",
  cost: "seg-cost",
};

function ms(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`;
}

export function Decision({ ranking }: { ranking: Ranked[] }) {
  if (!ranking.length) {
    return (
      <p className="state">
        No providers are registered. The gateway has nothing to route between.
      </p>
    );
  }

  const names = ranking.map((r) => r.provider);
  const scale = Math.max(...ranking.map((r) => r.score), 1);
  const anyUnexplained = ranking.some((r) => !decompose(r).explains);

  return (
    <>
      <div className="decision">
        {ranking.map((r, i) => {
          const parts = decompose(r);
          const colour = `var(--p${providerIndex(names, r.provider)})`;
          const chosen = i === 0 && r.breaker !== "open";
          return (
            <div
              key={r.provider}
              className={`lane${chosen ? " chosen" : ""}`}
              style={{ ["--seg" as string]: colour }}
            >
              <div className="who">
                <i className="dot" style={{ background: colour }} />
                <span className="name">{r.provider}</span>
                {chosen && <span className="pick">next</span>}
              </div>

              <div
                className="bar"
                role="img"
                aria-label={`${r.provider} score ${Math.round(r.score)}, ${
                  parts.explains
                    ? `latency ${Math.round(parts.latency)}, error ${Math.round(
                        parts.error,
                      )}, queue ${Math.round(parts.queue)}, price ${Math.round(parts.cost)}`
                    : "components unavailable"
                }`}
              >
                {parts.explains ? (
                  segments(parts, scale).map((s) => (
                    <span
                      key={s.key}
                      className={SEG_CLASS[s.key]}
                      style={{ width: `${s.width}%` }}
                      title={`${s.label} ${Math.round(s.value)} ms-equivalent`}
                    />
                  ))
                ) : (
                  <span
                    className="seg-latency"
                    style={{ width: `${(r.score / scale) * 100}%` }}
                    title={`score ${Math.round(r.score)}`}
                  />
                )}
              </div>

              <div className="figs">
                <span>
                  p50 <b>{ms(r.p50_ms)}</b>
                </span>
                <span>
                  p95 <b>{ms(r.p95_ms)}</b>
                </span>
                <span>
                  err <b>{(r.error_rate * 100).toFixed(1)}%</b>
                </span>
                {r.in_flight > 0 && (
                  <span>
                    busy <b>{r.in_flight}</b>
                  </span>
                )}
                <span className={`breaker ${r.breaker}`}>{r.breaker.replace("_", "-")}</span>
              </div>
            </div>
          );
        })}
      </div>

      <div className="key">
        <span>
          <i style={{ background: "var(--p0)" }} /> latency, a quarter tail and
          three quarters median
        </span>
        <span>
          <i style={{ background: "var(--down)", opacity: 0.85 }} /> error
          penalty, charged against that latency
        </span>
        <span>
          <i style={{ background: "var(--ink-faint)" }} /> queue depth
        </span>
        <span>
          <i style={{ background: "var(--p0)", opacity: 0.32 }} /> price, in
          milliseconds the router would trade for it
        </span>
        <span>shortest bar wins</span>
      </div>

      {anyUnexplained && (
        <p className="state err" style={{ marginTop: 14 }}>
          One or more scores could not be reconstructed from the published
          components, so those bars show the total only. The gateway is
          probably tuned with a different <code>tail_weight</code> or{" "}
          <code>error_penalty</code> than this console assumes.
        </p>
      )}
    </>
  );
}
