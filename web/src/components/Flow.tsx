/* Where the traffic went: one trunk per provider, thickness is share.
 *
 * This replaced the score bars as the hero of the live half for a reason
 * that only showed up when someone used the page. Three of the four
 * policies do not compute a score at all -- `static`, `round_robin` and
 * `random` pick by position, rotation and chance -- so a picture built on
 * score decomposition was blank or broken for three quarters of the thing
 * the console exists to compare.
 *
 * Share of traffic is true under every policy, and it is also the thing
 * worth looking at: switch from static to adaptive and the trunks visibly
 * rebalance.
 *
 * The breaker is drawn as a knife switch sitting in the trunk, because
 * that is what a circuit breaker is. Closed, the blade bridges the gap and
 * the line is continuous. Open, the blade is lifted and the gap is plainly
 * a gap -- no traffic is going down there, and you can see it rather than
 * read it off a label. Half-open is the blade resting against the contact,
 * dashed: the gateway is letting one request through to find out whether
 * the provider has recovered. Clicking a trunk throws its switch.
 *
 * The run into each node is straight for the last stretch so the switch
 * can sit in it; the rest is a cubic bezier, because three flows leaving
 * one point for three separated points cross each other as straight lines
 * and stop being followable.
 */

import { providerIndex } from "../lib/score";
import type { ProviderLive } from "../lib/api";

const W = 1000;
const ROW = 92;
const PAD_TOP = 24;
const GATE_X = 250;
const NODE_X = 640;
/* the straight run that holds the switch */
const SWITCH_A = NODE_X - 138;
const SWITCH_B = NODE_X - 62;

function ms(v: number): string {
  return v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`;
}

function Switch({
  x1,
  x2,
  y,
  state,
  colour,
}: {
  x1: number;
  x2: number;
  y: number;
  state: string;
  colour: string;
}) {
  // Closed: the blade lies in the line. Half-open: resting, drawn dashed.
  // Open: lifted clear of the far contact.
  const lift = state === "open" ? 34 : state === "half_open" ? 11 : 0;
  const bladeEnd = state === "open" ? x1 + (x2 - x1) * 0.82 : x2;
  const stroke = state === "open" ? "var(--down)" : colour;
  return (
    <g>
      <line
        x1={x1}
        y1={y}
        x2={bladeEnd}
        y2={y - lift}
        className="blade"
        stroke={stroke}
        strokeDasharray={state === "half_open" ? "4 3" : undefined}
      />
      <circle cx={x1} cy={y} r={5} className="term" stroke={stroke} />
      <circle cx={x2} cy={y} r={5} className="term" stroke={stroke} />
    </g>
  );
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
        {/* the trunks, widest first so a thin one is never hidden */}
        {[...providers]
          .map((p, i) => ({ p, i }))
          .sort((a, b) => b.p.requests - a.p.requests)
          .map(({ p, i }) => {
            const share = served ? p.requests / served : 0;
            const y = PAD_TOP + ROW * i + ROW / 2;
            const thickness = Math.max(1.5, share * 104);
            const colour = `var(--p${providerIndex(names, p.provider)})`;
            const open = p.breaker.state === "open";
            const mid = (GATE_X + SWITCH_A) / 2;
            return (
              <g key={p.provider}>
                <path
                  d={`M ${GATE_X} ${gateY} C ${mid} ${gateY}, ${mid} ${y}, ${SWITCH_A} ${y}`}
                  fill="none"
                  stroke={colour}
                  strokeWidth={thickness}
                  strokeOpacity={open ? 0.2 : 0.7}
                  strokeLinecap="butt"
                />
                <path
                  d={`M ${SWITCH_B} ${y} L ${NODE_X} ${y}`}
                  fill="none"
                  stroke={colour}
                  strokeWidth={thickness}
                  strokeOpacity={open ? 0.1 : 0.7}
                  strokeLinecap="butt"
                />
                <Switch
                  x1={SWITCH_A}
                  x2={SWITCH_B}
                  y={y}
                  state={p.breaker.state}
                  colour={colour}
                />
              </g>
            );
          })}

        {/* the switchboard itself */}
        <rect
          x={GATE_X - 108}
          y={gateY - 26}
          width={108}
          height={52}
          className="gate"
        />
        <text
          x={GATE_X - 54}
          y={gateY - 4}
          textAnchor="middle"
          className="gatename"
        >
          relay
        </text>
        <text
          x={GATE_X - 54}
          y={gateY + 14}
          textAnchor="middle"
          className="gatesub"
        >
          {policy}
        </text>
        <text
          x={GATE_X - 120}
          y={gateY + 4}
          textAnchor="end"
          className="inbound"
        >
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
              <rect x={NODE_X} y={y - 4} width={8} height={8} fill={colour} />
              <text x={NODE_X + 20} y={y - 10} className="pname">
                {p.provider}
              </text>
              <text x={NODE_X + 20} y={y + 8} className="pshare">
                {(share * 100).toFixed(0)}% of traffic,{" "}
                {p.requests.toLocaleString()} req
              </text>
              <text x={NODE_X + 20} y={y + 24} className="pstat">
                p50 {ms(p.p50_ms)}, p95 {ms(p.p95_ms)}
                {p.failures > 0 ? `, ${p.failures} failed` : ""}
              </text>
              <text
                x={NODE_X + 20}
                y={y + 40}
                className={`statelabel${state === "open" ? " open" : ""}`}
              >
                {state === "open"
                  ? "switch open — taken out of rotation"
                  : state === "half_open"
                    ? "switch resting — one request through to test it"
                    : "switch closed"}
              </text>
              {onToggleProvider && (
                <>
                  <rect
                    className="node-hit"
                    x={SWITCH_A - 10}
                    y={y - 30}
                    width={340}
                    height={60}
                    tabIndex={0}
                    role="button"
                    aria-label={`${
                      state === "open" ? "Close" : "Open"
                    } the switch on ${p.provider}`}
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
                    x={SWITCH_A - 10}
                    y={y - 30}
                    width={340}
                    height={60}
                  />
                </>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
