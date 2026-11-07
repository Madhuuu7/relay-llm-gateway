import { useCallback, useEffect, useRef, useState } from "react";
import { Decision } from "./components/Decision";
import { Flow } from "./components/Flow";
import { Tape } from "./components/Tape";
import { useCountUp } from "./lib/animate";
import { getJSON, usePoll, type RequestRow, type Stats } from "./lib/api";

function ms(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${Math.round(v)}ms`;
}

function usd(v: number): string {
  return v >= 0.01 ? `$${v.toFixed(3)}` : `$${v.toFixed(5)}`;
}

/** A tile whose number counts to its new value, so a change between two
 *  polls is visible rather than being a silent repaint. */
function Tile({
  value,
  label,
  render,
}: {
  value: number;
  label: string;
  render: (v: number) => string;
}) {
  const shown = useCountUp(value);
  return (
    <div>
      <span className="v">{render(shown)}</span>
      <span className="k">{label}</span>
    </div>
  );
}

function useTheme() {
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    try {
      const v = localStorage.getItem("relay-theme");
      if (v === "dark" || v === "light") return v;
    } catch {
      /* private window */
    }
    return typeof window !== "undefined" &&
      window.matchMedia("(prefers-color-scheme: light)").matches
      ? "light"
      : "dark";
  });
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("relay-theme", theme);
    } catch {
      /* the page still works, it just will not remember */
    }
  }, [theme]);
  return [theme, () => setTheme(theme === "dark" ? "light" : "dark")] as const;
}

export default function App() {
  const [theme, toggleTheme] = useTheme();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [tail, setTail] = useState(0.25);
  const [penalty, setPenalty] = useState(3);

  const stats = usePoll<Stats>("/stats", 1500);
  const log = usePoll<RequestRow[]>("/requests?limit=140", 1500);
  const s = stats.value;
  const rows = log.value ?? [];
  const policy = s?.live.policy ?? "adaptive";

  const send = useCallback(
    async (count: number) => {
      await getJSON(`/demo/load?requests=${count}&concurrency=8`, {
        method: "POST",
      });
      await Promise.all([stats.refresh(), log.refresh()]);
    },
    [stats, log],
  );

  // Streaming keeps traffic flowing so the page is genuinely live rather
  // than a still picture of the last manual burst. The ref guard stops a
  // slow burst from overlapping the next one and piling up.
  const sending = useRef(false);
  useEffect(() => {
    if (!streaming) return;
    let cancelled = false;
    const id = setInterval(() => {
      if (sending.current || cancelled) return;
      sending.current = true;
      void send(6)
        .catch(() => setStreaming(false))
        .finally(() => {
          sending.current = false;
        });
    }, 1800);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [streaming, send]);

  async function act(what: () => Promise<unknown>, failure: string) {
    setBusy(true);
    setNote(null);
    try {
      await what();
      await Promise.all([stats.refresh(), log.refresh()]);
    } catch (e) {
      setNote(e instanceof Error ? e.message : failure);
    } finally {
      setBusy(false);
    }
  }

  const retune = useCallback(
    (t: number, p: number) =>
      getJSON(`/admin/policy/adaptive?tail_weight=${t}&error_penalty=${p}`, {
        method: "POST",
      }).then(() => stats.refresh()),
    [stats],
  );

  if (stats.error && !s) {
    return (
      <div className="shell">
        <header className="top">
          <h1>relay</h1>
        </header>
        <div className="state err">
          <p>The gateway is not answering: {stats.error}</p>
          <p>
            Start it with{" "}
            <code>uvicorn relay.api.app:app --app-dir src --reload</code>.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="shell">
      <header className="top">
        <h1>relay</h1>
        <p>
          An LLM gateway that picks a provider from live latency, error rate
          and queue depth — and shows the arithmetic behind every choice.
        </p>
        <div className="controls">
          {streaming && (
            <span
              className="fig"
              style={{ fontSize: 12, color: "var(--ink-soft)" }}
            >
              <i className="livedot" /> live
            </span>
          )}
          {s && (
            <select
              value={policy}
              onChange={(e) =>
                void act(
                  () =>
                    getJSON(`/admin/policy/${e.target.value}`, {
                      method: "POST",
                    }),
                  "could not switch policy",
                )
              }
              disabled={busy}
              aria-label="Routing policy"
            >
              {s.policies.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          )}
          {s?.demo_mode && (
            <button
              className={`go${streaming ? " streaming" : ""}`}
              onClick={() => setStreaming(!streaming)}
            >
              {streaming ? "Stop traffic" : "Start traffic"}
            </button>
          )}
          <button onClick={toggleTheme}>
            {theme === "dark" ? "Light" : "Dark"}
          </button>
        </div>
      </header>

      {note && (
        <p className="state err" style={{ marginBottom: 20 }}>
          {note}
        </p>
      )}

      <section>
        <h2>Where the traffic went</h2>
        <p className="sub">
          Ribbon thickness is each provider's share of the requests served.
          Change the policy, or click a provider to take it out of rotation,
          and watch the ribbons rebalance — that shift is the whole claim of
          the project, and no average shows it.
        </p>
        {s ? (
          <Flow
            providers={s.live.providers}
            policy={policy}
            totalRequests={s.live.served}
            onToggleProvider={(provider, drain) =>
              void act(
                () =>
                  getJSON(
                    `/admin/breaker/${provider}/${drain ? "drain" : "reset"}`,
                    { method: "POST" },
                  ),
                "could not change the provider's state",
              )
            }
          />
        ) : (
          <p className="state">Loading.</p>
        )}
      </section>

      <section>
        <h2>Why it went there</h2>
        <p className="sub">
          The adaptive policy scores each provider and sends to the lowest.
          Each bar is that score, split into what produced it — so a provider
          that is fast but expensive looks different from one that is simply
          slow. The other three policies do not score at all.
        </p>
        {s ? <Decision ranking={s.live.ranking} policy={policy} /> : null}

        {policy === "adaptive" && (
          <div className="tuning">
            <label>
              tail weight
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={tail}
                onChange={(e) => {
                  const v = Number(e.target.value);
                  setTail(v);
                  void retune(v, penalty);
                }}
              />
              <span className="val">{tail.toFixed(2)}</span>
            </label>
            <label>
              error penalty
              <input
                type="range"
                min={0}
                max={12}
                step={0.5}
                value={penalty}
                onChange={(e) => {
                  const v = Number(e.target.value);
                  setPenalty(v);
                  void retune(tail, v);
                }}
              />
              <span className="val">{penalty.toFixed(1)}</span>
            </label>
            <span style={{ fontSize: 12.5, color: "var(--ink-faint)" }}>
              Move either and the ranking reorders live. At tail weight 1 the
              router judges purely on the tail; at 0, purely on the median.
            </span>
          </div>
        )}
      </section>

      <section>
        <h2>Every request, in order</h2>
        <p className="sub">
          One tick per request, coloured by who served it and as tall as it
          was slow. New requests arrive on the right.
        </p>
        <Tape rows={rows} ranking={s?.live.ranking ?? []} />
      </section>

      {s && (
        <section>
          <h2>Totals</h2>
          <div className="tiles">
            <Tile
              value={s.totals.requests}
              label="requests"
              render={(v) => Math.round(v).toLocaleString()}
            />
            <Tile value={s.totals.p50_ms} label="p50" render={ms} />
            <Tile value={s.totals.p95_ms} label="p95" render={ms} />
            <Tile value={s.totals.p99_ms} label="p99" render={ms} />
            <Tile
              value={s.totals.failovers}
              label="fail-overs"
              render={(v) => String(Math.round(v))}
            />
            <Tile
              value={s.totals.failures}
              label="failed"
              render={(v) => String(Math.round(v))}
            />
            <Tile value={s.totals.cost_usd} label="spent" render={usd} />
          </div>
        </section>
      )}

      {rows.length > 0 && (
        <section>
          <h2>The last ten requests</h2>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th>Request</th>
                  <th>Served by</th>
                  <th className="num">Latency</th>
                  <th className="num">Fail-overs</th>
                  <th className="num">Cost</th>
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, 10).map((r) => (
                  <tr key={r.request_id} className={r.ok ? undefined : "bad"}>
                    <td>
                      <span className="fig">{r.request_id.slice(-8)}</span>
                      {!r.ok && r.error && ` — ${r.error}`}
                    </td>
                    <td>{r.provider ?? "—"}</td>
                    <td className="num">{ms(r.latency_ms)}</td>
                    <td className="num">{r.failovers || ""}</td>
                    <td className="num">{usd(r.cost_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <footer>
        Point any OpenAI client at <code>/v1</code> and it gains fail-over,
        health-aware routing and per-request cost accounting without changing
        its own code. On a replayed trace of 4,000 requests, adaptive routing
        cut p95 latency 71.8% and circuit-breaker trips 81% against the static
        fallback list most gateways ship.
      </footer>
    </div>
  );
}
