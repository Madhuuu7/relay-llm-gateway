/* The decomposition, checked against a score the gateway actually produced.
 *
 * If this drifts from `routing/policies.py` the console draws a bar chart
 * that looks authoritative and is wrong, which is worse than drawing
 * nothing — so the real numbers from a live `/stats` response are pinned
 * here, and `explains` is the guard that turns the bars off rather than
 * letting them lie.
 */

import { describe, expect, it } from "vitest";
import { decompose, providerIndex, segments, type Ranked } from "./score";

// Captured from a running gateway: sim-fast, 60 demo requests.
const SIM_FAST: Ranked = {
  provider: "sim-fast",
  score: 892.6,
  p50_ms: 222.0,
  p95_ms: 351.0,
  error_rate: 0.0346,
  in_flight: 0,
  cost_term_ms: 612.0,
  breaker: "closed",
  reason: "measured",
};

describe("decomposing the routing score", () => {
  it("reproduces a score the gateway actually computed", () => {
    const p = decompose(SIM_FAST);
    // 0.25*351 + 0.75*222 = 254.25
    expect(p.latency).toBeCloseTo(254.25, 6);
    // 254.25 * 0.0346 * 3 = 26.39
    expect(p.error).toBeCloseTo(26.391, 2);
    expect(p.queue).toBe(0);
    expect(p.cost).toBe(612);
    expect(p.total).toBeCloseTo(892.64, 1);
    expect(Math.abs(p.residual)).toBeLessThan(0.1);
    expect(p.explains).toBe(true);
  });

  it("weights the tail at a quarter, not the median at a quarter", () => {
    // Getting this backwards still produces a plausible-looking bar, so it
    // is pinned explicitly: a provider with a bad tail and a good median
    // must score worse than the reverse.
    const badTail = decompose({ ...SIM_FAST, p50_ms: 200, p95_ms: 4000, error_rate: 0 });
    const badMedian = decompose({ ...SIM_FAST, p50_ms: 4000, p95_ms: 200, error_rate: 0 });
    expect(badTail.latency).toBeLessThan(badMedian.latency);
    expect(badTail.latency).toBeCloseTo(0.25 * 4000 + 0.75 * 200, 6);
  });

  it("charges the error penalty against latency, not as a flat cost", () => {
    // The penalty scales with how slow the provider already is, so a flaky
    // fast provider is punished less than a flaky slow one.
    const fast = decompose({ ...SIM_FAST, p50_ms: 100, p95_ms: 100, error_rate: 0.1 });
    const slow = decompose({ ...SIM_FAST, p50_ms: 1000, p95_ms: 1000, error_rate: 0.1 });
    expect(slow.error).toBeCloseTo(fast.error * 10, 6);
  });

  it("counts queue depth", () => {
    const busy = decompose({ ...SIM_FAST, in_flight: 4 });
    expect(busy.queue).toBe(200);
    expect(busy.total).toBeCloseTo(decompose(SIM_FAST).total + 200, 6);
  });

  it("refuses to explain a score it cannot reconstruct", () => {
    // A server tuned with different constants, or a future term added to
    // the policy. The console must notice and fall back to the total.
    const mystery = decompose({ ...SIM_FAST, score: 5000 });
    expect(mystery.explains).toBe(false);
    expect(Math.abs(mystery.residual)).toBeGreaterThan(1);
  });

  it("honours non-default constants when told about them", () => {
    const p = decompose(SIM_FAST, { tailWeight: 0.5 });
    expect(p.latency).toBeCloseTo(0.5 * 351 + 0.5 * 222, 6);
  });
});

describe("bar segments", () => {
  it("drops components that contribute nothing", () => {
    const parts = decompose({ ...SIM_FAST, error_rate: 0, in_flight: 0 });
    const keys = segments(parts, parts.total).map((s) => s.key);
    expect(keys).toEqual(["latency", "cost"]);
  });

  it("scales against the widest score so rows are comparable", () => {
    const parts = decompose(SIM_FAST);
    const half = segments(parts, parts.total * 2);
    const full = segments(parts, parts.total);
    expect(half[0].width).toBeCloseTo(full[0].width / 2, 6);
    // And the full-scale widths sum to 100%, so the bar fills its track.
    expect(full.reduce((a, s) => a + s.width, 0)).toBeCloseTo(100, 4);
  });

  it("does not divide by zero on an empty gateway", () => {
    const parts = decompose({ ...SIM_FAST, p50_ms: 0, p95_ms: 0, error_rate: 0, cost_term_ms: 0, score: 0 });
    expect(segments(parts, 0)).toEqual([]);
  });
});

describe("provider colours", () => {
  it("does not change when a provider's rank changes", () => {
    // Keyed on the sorted names, not on ranking order. Keyed on rank, a
    // provider would swap colour the moment it started losing -- which is
    // exactly when you are staring at the tape trying to see where the
    // traffic moved, and the one moment the colours must hold still.
    const winning = ["sim-fast", "sim-cheap", "sim-balanced"];
    const losing = ["sim-balanced", "sim-cheap", "sim-fast"];
    for (const name of winning) {
      expect(providerIndex(winning, name)).toBe(providerIndex(losing, name));
    }
  });

  it("gives each provider a distinct slot", () => {
    const names = ["sim-fast", "sim-cheap", "sim-balanced"];
    const slots = names.map((n) => providerIndex(names, n));
    expect(new Set(slots).size).toBe(names.length);
  });

  it("does not throw on a provider it has not seen", () => {
    expect(providerIndex(["a", "b"], "c")).toBe(0);
  });
});
