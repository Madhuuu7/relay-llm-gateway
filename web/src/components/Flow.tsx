/* Where the traffic went: one ribbon per provider, thickness is share.
 *
 * This replaced the score bars as the hero for a reason that only showed up
 * when someone used the page. Three of the four policies do not compute a
 * score at all -- `static`, `round_robin` and `random` pick by position,
 * rotation and chance -- so a hero built on score decomposition was blank
 * or broken for three quarters of the thing the console exists to compare.
 *
 * Share of traffic is true under every policy, and it is also the thing
 * worth looking at: switch from static to adaptive and the ribbons visibly
 * rebalance. That is the project's claim, in one picture, with no reading
 * required.
 *
 * The ribbons are cubic beziers rather than straight lines because three
 * flows leaving one point and arriving at three separated points cross each
 * other as straight lines and stop being followable.
 */

import { providerIndex } from "../lib/score";
import type { ProviderLive } from "../lib/api";

const W = 1000;
const ROW = 86;
const PAD_TOP = 22;
const GATE_X = 268;
const NODE_X = 660;

function ms(v: number): string {
  return v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`;
}

export function Flow({
  providers,
  policy,
  totalRequests,
  onToggleProvider,
}: {
  providers: ProviderLive[];
  policy: string;
  totalRequests: number;
  /** Drain a healthy provider, or restore a drained one. */
  onToggleProvider?: (provider: string, open: boolean) => void;
}) {
  if (!providers.length) {
    return <p className="state">No providers are registered.</p>;
  }

  const names = providers.map((p) => p.provider);
  const served = providers.reduce((a, p) => a + p.requests, 0);
  const height = PAD_TOP * 2 + ROW * providers.length;
  const gateY = height / 2;

  return (
    <div className="flowwrap">
      <svg
        className="flow"
        viewBox={`0 0 ${W} ${height}`}
        role="img"
        aria-label={`Traffic split under the ${policy} policy across ${providers.length} providers`}
      >
        {/* the ribbons, widest first so a thin one is never hidden */}
        {[...providers]
          .map((p, i) => ({ p, i }))
          .sort((a, b) => b.p.requests - a.p.requests)
          .map(({ p, i }) => {
            const share = served ? p.requests / served : 0;
            const y = PAD_TOP + ROW * i + ROW / 2;
            const thickness = Math.max(1.5, share * 112);
            const colour = `var(--p${providerIndex(names, p.provider)})`;
            const open = p.breaker.state === "open";
            return (
              <path
                key={p.provider}
                d={`M ${GATE_X} ${gateY} C ${(GATE_X + NODE_X) / 2} ${gateY}, ${
                  (GATE_X + NODE_X) / 2
                } ${y}, ${NODE_X} ${y}`}
                fill="none"
                stroke={open ? "var(--down)" : colour}
                strokeWidth={thickness}
                strokeOpacity={open ? 0.25 : 0.5}
                strokeLinecap="round"
                strokeDasharray={open ? "7 7" : undefined}
              />
            );
          })}

        {/* the gateway */}
        <rect
          x={GATE_X - 104}
          y={gateY - 25}
          width={104}
          height={50}
          className="gate"
        />
        <text x={GATE_X - 52} y={gateY - 4} textAnchor="middle" className="gatename">
          relay
        </text>
        <text x={GATE_X - 52} y={gateY + 13} textAnchor="middle" className="gatesub">
          {policy}
        </text>
        <text x={GATE_X - 116} y={gateY + 4} textAnchor="end" className="inbound">
          {totalRequests.toLocaleString()} in
        </text>

        {/* the providers */}
        {providers.map((p, i) => {
          const y = PAD_TOP + ROW * i + ROW / 2;
          const share = served ? p.requests / served : 0;
          const colour = `var(--p${providerIndex(names, p.provider)})`;
          const state = p.breaker.state;
          return (
            <g key={p.provider}>
              <rect x={NODE_X} y={y - 3} width={6} height={6} fill={colour} />
              <text x={NODE_X + 18} y={y - 10} className="pname">
                {p.provider}
              </text>
              <text x={NODE_X + 18} y={y + 8} className="pshare">
                {(share * 100).toFixed(0)}% · {p.requests.toLocaleString()} req
              </text>
              <text x={NODE_X + 18} y={y + 24} className="pstat">
                p50 {ms(p.p50_ms)} · p95 {ms(p.p95_ms)}
                {p.failures > 0 ? ` · ${p.failures} failed` : ""}
              </text>
              {onToggleProvider && (
                <>
                  <rect
                    className="node-hit"
                    x={NODE_X - 8}
                    y={y - 24}
                    width={330}
                    height={48}
                    tabIndex={0}
                    role="button"
                    aria-label={`${
                      state === "open" ? "Restore" : "Drain"
                    } ${p.provider}`}
                    onClick={() => onToggleProvider(p.provider, state !== "open")}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onToggleProvider(p.provider, state !== "open");
                      }
                    }}
                  />
                  <rect
                    className="node-ring"
                    x={NODE_X - 8}
                    y={y - 24}
                    width={330}
                    height={48}
                  />
                  <text x={NODE_X + 18} y={y + 38} className="drainhint">
                    {state === "open"
                      ? "click to put back in rotation"
                      : "click to take out of rotation"}
                  </text>
                </>
              )}
              <g transform={`translate(${W - 118} ${y - 9})`}>
                <rect
                  width={108}
                  height={18}
                  className={`brk brk-${state}`}
                  rx={0}
                />
                <text x={54} y={13} textAnchor="middle" className={`brktext brk-${state}`}>
                  breaker {state.replace("_", "-")}
                </text>
              </g>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
