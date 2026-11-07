/* Taking the routing score apart.
 *
 * The adaptive policy ranks providers by one number, and that number is the
 * whole argument of this project — so the console shows what it is made of
 * rather than just its value. From `routing/policies.py`:
 *
 *     latency = tail_weight * p95 + (1 - tail_weight) * p50
 *     score   = latency
 *             + latency * error_rate * error_penalty
 *             + in_flight * queue_penalty_ms
 *             + cost_term
 *
 * Lower wins. The useful thing about seeing it split is that the parts
 * disagree: the fastest provider is often the one carrying the largest cost
 * term, and a reader who only sees the total cannot tell a provider that is
 * slow from one that is merely expensive.
 *
 * The server sends `score`, `p50_ms`, `p95_ms`, `error_rate`, `in_flight`
 * and `cost_term_ms`, which is everything the formula needs except the two
 * config constants. Those are re-derived here rather than hard-coded, so a
 * server tuned with a different `tail_weight` does not quietly render a
 * decomposition that does not add up — `residual` is the check, and the
 * component bars are only drawn when it is negligible.
 */

export type Ranked = {
  provider: string;
  score: number;
  p50_ms: number;
  p95_ms: number;
  error_rate: number;
  in_flight: number;
  cost_term_ms: number;
  breaker: string;
  reason: string;
};

export type Parts = {
  latency: number;
  error: number;
  queue: number;
  cost: number;
  total: number;
  /** score - (latency + error + queue + cost). Should be ~0. */
  residual: number;
  /** False when the server's constants differ from the ones assumed here. */
  explains: boolean;
};

/** Defaults from `AdaptiveConfig`; see the module note on why they are not
 *  treated as certain. */
export const TAIL_WEIGHT = 0.25;
export const ERROR_PENALTY = 3.0;
export const QUEUE_PENALTY_MS = 50.0;

export function decompose(
  r: Ranked,
  {
    tailWeight = TAIL_WEIGHT,
    errorPenalty = ERROR_PENALTY,
    queuePenaltyMs = QUEUE_PENALTY_MS,
  } = {},
): Parts {
  const latency = tailWeight * r.p95_ms + (1 - tailWeight) * r.p50_ms;
  const error = latency * r.error_rate * errorPenalty;
  const queue = r.in_flight * queuePenaltyMs;
  const cost = r.cost_term_ms;
  const total = latency + error + queue + cost;
  const residual = r.score - total;
  return {
    latency,
    error,
    queue,
    cost,
    total,
    residual,
    // A millisecond of slack on a score measured in hundreds, or 1% of it,
    // whichever is larger. Beyond that the split is not this formula and
    // the console falls back to the single number it was given.
    explains: Math.abs(residual) <= Math.max(1, Math.abs(r.score) * 0.01),
  };
}

/** Widths as percentages of the widest score on screen, so the bars are
 *  comparable between providers rather than each filling its own row. */
export function segments(parts: Parts, scale: number) {
  const pct = (v: number) => (scale > 0 ? (v / scale) * 100 : 0);
  return [
    { key: "latency", label: "latency", value: parts.latency, width: pct(parts.latency) },
    { key: "error", label: "error penalty", value: parts.error, width: pct(parts.error) },
    { key: "queue", label: "queue", value: parts.queue, width: pct(parts.queue) },
    { key: "cost", label: "price", value: parts.cost, width: pct(parts.cost) },
  ].filter((s) => s.value > 0.0001);
}

/** A stable colour index per provider, by position in the ranking the
 *  server sends. Hashing the name instead would reshuffle the palette
 *  whenever a provider is renamed. */
export function providerIndex(names: string[], name: string): number {
  const at = names.indexOf(name);
  return at < 0 ? 0 : at % 4;
}
