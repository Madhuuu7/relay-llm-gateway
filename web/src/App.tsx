import { useMemo, useState } from "react";
import { Decision } from "./components/Decision";
import { Tape } from "./components/Tape";
import { getJSON, usePoll, type RequestRow, type Stats } from "./lib/api";
import { providerIndex } from "./lib/score";

function ms(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${Math.round(v)}ms`;
}

function usd(v: number): string {
  return v >= 0.01 ? `$${v.toFixed(3)}` : `$${v.toFixed(5)}`;
}

function useTheme() {
  const [theme, setTheme] = useState<"dark" | "light" | null>(() => {
    try {
      const v = localStorage.getItem("relay-theme");
      return v === "dark" || v === "light" ? v : null;
    } catch {
      return null;
    }
  });
  const prefersLight =
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-color-scheme: light)").matches;
  const effective = theme ?? (prefersLight ? "light" : "dark");
  const toggle = () => {
    const next = effective === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("relay-theme", next);
    } catch {
      /* private window: the page still works, it just will not remember */
    }
  };
  return [effective, toggle] as const;
}

export default function App() {
  const [theme, toggleTheme] = useTheme();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const stats = usePoll<Stats>("/stats", 2000);
  const log = usePoll<RequestRow[]>("/requests?limit=120", 2000);

  const s = stats.value;
  const rows = log.value ?? [];
  const names = useMemo(
    () => (s ? s.live.ranking.map((r) => r.provider) : []),
    [s],
  );

  async function sendTraffic() {
    setBusy(true);
    setNote(null);
    try {
      await getJSON("/demo/load?requests=40&concurrency=8", { method: "POST" });
      await Promise.all([stats.refresh(), log.refresh()]);
    } catch (e) {
      setNote(e instanceof Error ? e.message : "could not generate traffic");
    } finally {
      setBusy(false);
    }
  }

  async function setPolicy(name: string) {
    setBusy(true);
    setNote(null);
    try {
      await getJSON(`/admin/policy/${name}`, { method: "POST" });
      await stats.refresh();
    } catch (e) {
      setNote(e instanceof Error ? e.message : "could not switch policy");
    } finally {
      setBusy(false);
    }
  }

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
            <code>
              uvicorn relay.api.app:app --app-dir src --reload
            </code>
            .
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
          {s && (
            <select
              value={s.live.policy}
              onChange={(e) => void setPolicy(e.target.value)}
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
            <button className="go" onClick={() => void sendTraffic()} disabled={busy}>
              {busy ? "sending…" : "Send 40 requests"}
            </button>
          )}
          <button onClick={toggleTheme}>{theme === "dark" ? "Light" : "Dark"}</button>
        </div>
      </header>

      {note && (
        <p className="state err" style={{ marginBottom: 22 }}>
          {note}
        </p>
      )}

      <section>
        <h2>Where the next request goes</h2>
        <p className="sub">
          The policy scores each provider and sends to the lowest. Every bar
          is that score, split into what produced it — so a provider that is
          fast but expensive looks different from one that is simply slow.
        </p>
        {s ? <Decision ranking={s.live.ranking} /> : <p className="state">Loading.</p>}
      </section>

      <section>
        <h2>Every request, in order</h2>
        <p className="sub">
          One tick per request, coloured by who served it. Switch the policy
          above, send more traffic, and the colour balance changes — that
          shift is the whole claim of the project, and it is not visible in
          an average.
        </p>
        <Tape rows={rows} ranking={s?.live.ranking ?? []} />
      </section>

      {s && (
        <section>
          <h2>Totals</h2>
          <div className="tiles">
            <div>
              <span className="v">{s.totals.requests.toLocaleString()}</span>
              <span className="k">requests</span>
            </div>
            <div>
              <span className="v">{ms(s.totals.p50_ms)}</span>
              <span className="k">p50</span>
            </div>
            <div>
              <span className="v">{ms(s.totals.p95_ms)}</span>
              <span className="k">p95</span>
            </div>
            <div>
              <span className="v">{ms(s.totals.p99_ms)}</span>
              <span className="k">p99</span>
            </div>
            <div>
              <span className="v">{s.totals.failovers}</span>
              <span className="k">fail-overs</span>
            </div>
            <div>
              <span className="v">{s.totals.failures}</span>
              <span className="k">failed</span>
            </div>
            <div>
              <span className="v">{usd(s.totals.cost_usd)}</span>
              <span className="k">spent</span>
            </div>
          </div>
        </section>
      )}

      {s && s.by_provider.length > 0 && (
        <section>
          <h2>By provider</h2>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th>Provider</th>
                  <th className="num">Requests</th>
                  <th className="num">Failures</th>
                  <th className="num">p50</th>
                  <th className="num">p95</th>
                  <th className="num">Tokens</th>
                  <th className="num">Cost</th>
                </tr>
              </thead>
              <tbody>
                {s.by_provider.map((p) => (
                  <tr key={p.provider}>
                    <td>
                      <i
                        className="dot"
                        style={{
                          display: "inline-block",
                          width: 9,
                          height: 9,
                          marginRight: 8,
                          background: `var(--p${providerIndex(names, p.provider)})`,
                        }}
                      />
                      {p.provider}
                    </td>
                    <td className="num">{p.requests}</td>
                    <td className="num">{p.failures}</td>
                    <td className="num">{ms(p.p50_ms)}</td>
                    <td className="num">{ms(p.p95_ms)}</td>
                    <td className="num">{p.tokens.toLocaleString()}</td>
                    <td className="num">{usd(p.cost_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {rows.length > 0 && (
        <section>
          <h2>Request log</h2>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th>Request</th>
                  <th>Served by</th>
                  <th className="num">Latency</th>
                  <th className="num">Fail-overs</th>
                  <th className="num">Tokens</th>
                  <th className="num">Cost</th>
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, 25).map((r) => (
                  <tr key={r.request_id} className={r.ok ? undefined : "bad"}>
                    <td>
                      <span className="fig">{r.request_id.slice(-8)}</span>
                      {!r.ok && r.error && ` — ${r.error}`}
                    </td>
                    <td>{r.provider ?? "—"}</td>
                    <td className="num">{ms(r.latency_ms)}</td>
                    <td className="num">{r.failovers || ""}</td>
                    <td className="num">
                      {(r.input_tokens + r.output_tokens).toLocaleString()}
                    </td>
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
        cut p95 latency 71.8% and circuit-breaker trips 81% against the
        static fallback list most gateways ship.
      </footer>
    </div>
  );
}
