/* Why the router chose what it chose — when the policy actually chooses.
 *
 * Only the adaptive policy computes a score, and that score is a sum of
 * four terms that routinely disagree: the fastest provider is usually the
 * one carrying the largest price term. Showing the split is the thing this
 * console can do that a per-provider latency chart cannot.
 *
 * Under `static`, `round_robin` and `random` there is nothing to show,
 * because there is no arithmetic -- the gateway returns nulls and says so
 * in `reason`. This panel says that plainly rather than rendering an empty
 * chart, and the honest version of it is a sentence explaining what the
 * policy does instead.
 */

import { decompose, providerIndex, ranks, segments, type Ranked } from "../lib/score";

const SEG_CLASS: Record<string, string> = {
  latency: "seg-latency",
  error: "seg-error",
  queue: "seg-queue",
  cost: "seg-cost",
};

const UNSCORED: Record<string, string> = {
  static:
    "Static routing tries the providers in a fixed order and moves on only when one fails. It has no score: a provider that is slow but still returning 200s stays at the head of the list forever.",
  round_robin:
    "Round robin rotates through the providers in turn, ignoring how any of them is behaving. It has no score.",
  random:
    "Random picks uniformly at random. It has no score, and it is here as a control — it spreads load without measuring anything.",
};

function ms(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`;
}

export function Decision({ ranking, policy }: { ranking: Ranked[]; policy: string }) {
  const scored = ranking.filter(ranks);

  if (!scored.length) {
    return (
      <p className="state">
        {UNSCORED[policy] ??
          `The ${policy} policy does not rank providers by a score, so there is nothing to break down.`}{" "}
        Switch to <strong>adaptive</strong> to see the arithmetic.
      </p>
    );
  }

  const names = ranking.map((r) => r.provider);
  const scale = Math.max(...scored.map((r) => r.score ?? 0), 1);
  const anyUnexplained = scored.some((r) => !decompose(r).explains);

  return (
    <>
      <div className="decision">
        {scored.map((r, i) => {
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
                aria-label={`${r.provider} score ${Math.round(r.score ?? 0)}`}
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
                    style={{ width: `${((r.score ?? 0) / scale) * 100}%` }}
                    title={`score ${Math.round(r.score ?? 0)}`}
                  />
                )}
              </div>

              <div className="figs">
                <span>
                  score <b>{Math.round(r.score ?? 0)}</b>
                </span>
                <span>
                  p50 <b>{ms(r.p50_ms)}</b>
                </span>
                <span>
                  p95 <b>{ms(r.p95_ms)}</b>
                </span>
                <span>
                  err <b>{((r.error_rate ?? 0) * 100).toFixed(1)}%</b>
                </span>
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
          A score could not be reconstructed from its published components,
          so that bar shows the total only. The gateway is probably tuned
          with a different <code>tail_weight</code> or{" "}
          <code>error_penalty</code> than this console assumes.
        </p>
      )}
    </>
  );
}
