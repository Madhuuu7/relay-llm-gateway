/* The gateway's shapes, and a poll that does not pile up.
 *
 * The console refreshes while traffic is running, and a slow response must
 * not cause two requests to overlap or the panel to flash through a loading
 * state on every tick. So `usePoll` keeps the last good value until the next
 * one lands, and skips a tick if the previous one is still in flight.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { Ranked } from "./score";

export type Breaker = {
  provider: string;
  state: string;
  trips: number;
  rejected: number;
  consecutive_failures: number;
  cooldown_remaining_s: number;
};

export type ProviderLive = {
  provider: string;
  requests: number;
  failures: number;
  p50_ms: number;
  p95_ms: number;
  mean_ms: number;
  error_rate_ewma: number;
  error_rate_observed: number;
  cost_usd: number;
  tokens: number;
  in_flight: number;
  breaker: Breaker;
  models: string[];
};

export type Totals = {
  requests: number;
  failures: number;
  cost_usd: number;
  tokens: number;
  failovers: number;
  mean_ms: number;
  p50_ms: number;
  p95_ms: number;
  p99_ms: number;
};

export type Bucket = {
  t: number;
  requests: number;
  failures: number;
  p95_ms: number;
  cost_usd: number;
};

export type Stats = {
  demo_mode: boolean;
  live: {
    policy: string;
    served: number;
    failed: number;
    providers: ProviderLive[];
    ranking: Ranked[];
  };
  totals: Totals;
  by_provider: {
    provider: string;
    requests: number;
    failures: number;
    p50_ms: number;
    p95_ms: number;
    cost_usd: number;
    tokens: number;
  }[];
  timeline: Bucket[];
  policies: string[];
};

export type Attempt = {
  provider: string;
  ok: boolean;
  latency_ms: number | null;
  error: string | null;
  skipped: string | null;
};

export type RequestRow = {
  request_id: string;
  ts: number;
  policy: string;
  ok: boolean;
  provider: string | null;
  model: string | null;
  latency_ms: number | null;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  failovers: number;
  error: string | null;
  attempts: Attempt[];
};

export async function getJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { accept: "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error((body as { detail?: string }).detail ?? `HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

export function usePoll<T>(path: string, intervalMs: number) {
  const [value, setValue] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);

  const tick = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      setValue(await getJSON<T>(path));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "the gateway did not answer");
    } finally {
      inFlight.current = false;
    }
  }, [path]);

  useEffect(() => {
    void tick();
    const id = setInterval(() => void tick(), intervalMs);
    return () => clearInterval(id);
  }, [tick, intervalMs]);

  return { value, error, refresh: tick };
}
