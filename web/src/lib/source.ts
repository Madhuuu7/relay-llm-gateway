/* Where the console gets its data when there is no gateway behind it.
 *
 * The console is published two ways. Served by the gateway it shows a live
 * process and the controls really route traffic. Published as a static
 * page there is no process at all.
 *
 * The two halves are not equally honest and the page treats them
 * differently. The replay benchmark *is* reproducible by construction --
 * provider latency and failure are derived from a hash of (provider,
 * position), so the four policies see identical upstreams and the same
 * numbers come out on any machine. A recording of that is the result.
 *
 * The live panels are not. A snapshot of gateway state is one moment of
 * simulated traffic and nothing more, so on the static page it is labelled
 * as a frozen sample and the controls that would pretend to work -- start
 * traffic, drain a provider, the tuning sliders -- are hidden rather than
 * left to fail.
 *
 * Probe once, cache the answer. Retrying a dead endpoint on every click
 * would make the static page feel broken rather than static.
 */

export type Mode = "live" | "recorded";

let resolved: Mode | null = null;
// The promise, not only the answer. Every view asks on first paint, and
// they all ask before any of them has an answer to cache -- so a page with
// six panels fired six probes at a dead endpoint and left six 404s in the
// console of anyone who opened it.
let asking: Promise<Mode> | null = null;

/** Ask the runtime whether it is there. */
export async function detect(): Promise<Mode> {
  if (resolved) return resolved;
  if (!asking) {
    asking = (async () => {
      try {
        const response = await fetch("health", {
          headers: { accept: "application/json" },
          signal: AbortSignal.timeout(4000),
        });
        resolved = response.ok ? "live" : "recorded";
      } catch {
        resolved = "recorded";
      }
      asking = null;
      return resolved;
    })();
  }
  return asking;
}

/** Force a mode, for tests. */
export function setMode(mode: Mode | null): void {
  resolved = mode;
  asking = null;
}

/**
 * Fetch from the runtime, or from the recording if there is no runtime.
 *
 * Paths are relative so the page works under a sub-path -- GitHub Pages
 * serves a project site from `/<repo>/`, and a leading slash would send
 * every request to the domain root.
 */
export async function read<T>(live: string, recorded: string): Promise<T> {
  const mode = await detect();
  const path = mode === "live" ? live : `demo/${recorded}`;
  const response = await fetch(path, { headers: { accept: "application/json" } });
  if (!response.ok) {
    throw new Error(
      mode === "live"
        ? `the runtime answered ${response.status} for ${live}`
        : `the recording ${recorded} is missing from this build`,
    );
  }
  return (await response.json()) as T;
}
