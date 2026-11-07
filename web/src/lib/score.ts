/* Taking the routing score apart — when there is a score to take apart.
 *
 * The adaptive policy ranks providers by one number, and that number is the
 * whole argument of this project:
 *
 *     latency = tail_weight * p95 + (1 - tail_weight) * p50
 *     score   = latency
 *             + latency * error_rate * error_penalty
 *             + in_flight * queue_penalty_ms
 *             + cost_term
 *
 * Lower wins. Seeing it split matters because the parts disagree: the
 * fastest provider is usually the one carrying the largest price term,
 * which is the trade a static fallback list cannot express.
 *
 * **The other three policies do not score.** `static`, `round_robin` and
 * `random` pick by position, rotation or chance, and the gateway says so by
 * sending `reason` set to the policy name and every measurement field null.
 * An earlier version of this console assumed the adaptive shape and crashed
 * on the first null the moment anyone touched the policy dropdown — so
 * `ranks()` is the guard, and nothing downstream reads a score without
 * asking it first.
 */

export type Ranked = {
  provider: string;
  score: number | null;
  p50_ms: number | null;
  p95_ms: number | null;
  error_rate: number | null;
  in_flight: number | null;
  cost_term_ms: number | null;
  breaker: string | null;
  reason: string | null;
};

export type Parts = {
  latency: number;
  error: number;
  queue: number;
  cost: number;
  total: number;
  /** score - (latency + error + queue + cost). Should be ~0. */
  residual: number;
  /** False when the numbers are absent or the constants do not match. */
  explains: boolean;
};

export const TAIL_WEIGHT = 0.25;
export const ERROR_PENALTY = 3.0;
export const QUEUE_PENALTY_MS = 50.0;

const NOTHING: Parts = {
  latency: 0,
  error: 0,
  queue: 0,
  cost: 0,
  total: 0,
  residual: 0,
  explains: false,
};

/** Whether this entry carries a real score, rather than a placeholder from
 *  a policy that does not rank. */
export function ranks(r: Ranked): boolean {
  return (
    r.reason === "measured" &&
    typeof r.score === "number" &&
    typeof r.p50_ms === "number" &&
    typeof r.p95_ms === "number" &&
    typeof r.cost_term_ms === "number"
  );
}

export function decompose(
  r: Ranked,
  {
    tailWeight = TAIL_WEIGHT,
    errorPenalty = ERROR_PENALTY,
    queuePenaltyMs = QUEUE_PENALTY_MS,
  } = {},
): Parts {
  if (!ranks(r)) return NOTHING;
  const p50 = r.p50_ms as number;
  const p95 = r.p95_ms as number;
  const latency = tailWeight * p95 + (1 - tailWeight) * p50;
  const error = latency * (r.error_rate ?? 0) * errorPenalty;
  const queue = (r.in_flight ?? 0) * queuePenaltyMs;
  const cost = r.cost_term_ms as number;
  const total = latency + error + queue + cost;
  const score = r.score as number;
  const residual = score - total;
  return {
    latency,
    error,
    queue,
    cost,
    total,
    residual,
    explains: Math.abs(residual) <= Math.max(1, Math.abs(score) * 0.01),
  };
}

export function segments(parts: Parts, scale: number) {
  const pct = (v: number) => (scale > 0 ? (v / scale) * 100 : 0);
  return [
    { key: "latency", label: "latency", value: parts.latency, width: pct(parts.latency) },
    { key: "error", label: "error penalty", value: parts.error, width: pct(parts.error) },
    { key: "queue", label: "queue", value: parts.queue, width: pct(parts.queue) },
    { key: "cost", label: "price", value: parts.cost, width: pct(parts.cost) },
  ].filter((s) => s.value > 0.0001);
}

/** A stable colour index per provider. Sorted by name rather than by the
 *  ranking order, so a provider does not change colour the moment it starts
 *  losing — which would make the tape unreadable exactly when it matters. */
export function providerIndex(names: string[], name: string): number {
  const at = [...names].sort().indexOf(name);
  return at < 0 ? 0 : at % 4;
}
