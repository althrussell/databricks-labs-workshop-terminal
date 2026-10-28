// Browser-side live-stream (SSE) and WebSocket network checker.
//
// Reads /api/network-check/stream using fetch() + ReadableStream (the same way
// Omnigent reads its session stream, not EventSource) so corporate proxies
// that hold back small chunks are detected before an attendee starts a chat.

import { pingAppSocket } from "./events";

export type NetworkVerdict = "ok" | "slow" | "blocked";
export type NetworkStatusKind = "checking" | NetworkVerdict;

export interface NetworkSample {
  seq: number;
  server_elapsed_ms: number;
  client_elapsed_ms: number;
  flush?: boolean;
}

export interface NetworkCheckResult {
  verdict: NetworkVerdict;
  checked_at: string;
  first_event_ms: number | null;
  max_lag_ms: number | null;
  received_count: number;
  expected_count: number;
  clumped: boolean;
  ws_rtt_ms: number | null;
  http_rtt_ms: number | null;
  browser_family: string;
  network_label?: string;
}

export const EXPECTED_EVENT_COUNT = 11;
export const RECHECK_COOLDOWN_MS = 10 * 60 * 1000;

export function detectBrowserFamily(ua = typeof navigator !== "undefined" ? navigator.userAgent : ""): string {
  const lower = ua.toLowerCase();
  if (lower.includes("edg/")) return "Edge";
  if (lower.includes("chrome/") && !lower.includes("chromium")) return "Chrome";
  if (lower.includes("firefox/")) return "Firefox";
  if (lower.includes("safari/") && !lower.includes("chrome/")) return "Safari";
  return "Browser";
}

export function evaluateSamples(
  samples: NetworkSample[],
  expectedCount = EXPECTED_EVENT_COUNT,
  wsRttMs: number | null = 50,
  httpRttMs: number | null = 50
): Omit<NetworkCheckResult, "checked_at"> {
  const smallSamples = samples.filter((s) => !s.flush);
  const receivedCount = samples.length;
  const firstEventMs = samples.length > 0 ? Math.max(0, Math.round(samples[0].client_elapsed_ms)) : null;

  let maxLagMs: number | null = null;
  let maxGapMs = 0;
  let prevClientMs = 0;
  for (const sample of samples) {
    const lag = Math.max(0, Math.round(sample.client_elapsed_ms - sample.server_elapsed_ms));
    if (maxLagMs === null || lag > maxLagMs) maxLagMs = lag;
    const gap = Math.max(0, Math.round(sample.client_elapsed_ms - prevClientMs));
    if (gap > maxGapMs) maxGapMs = gap;
    prevClientMs = sample.client_elapsed_ms;
  }

  // Clumping: proxy holds small events until the 32 KB flush or stream close,
  // releasing multiple scheduled events inside a single <750ms burst after >5s.
  let clumped = false;
  if (smallSamples.length >= 4) {
    const firstSmall = smallSamples[0].client_elapsed_ms;
    const lastSmall = smallSamples[smallSamples.length - 1].client_elapsed_ms;
    const serverSpan =
      smallSamples[smallSamples.length - 1].server_elapsed_ms - smallSamples[0].server_elapsed_ms;
    if (serverSpan >= 5000 && lastSmall - firstSmall < 750) {
      clumped = true;
    }
  }

  let verdict: NetworkVerdict = "ok";
  if (
    receivedCount === 0 ||
    clumped ||
    (firstEventMs !== null && firstEventMs > 15000) ||
    (maxLagMs !== null && maxLagMs >= 15000) ||
    maxGapMs > 45000
  ) {
    verdict = "blocked";
  } else if (
    // The verdict grades live updates (the stream). A WebSocket RTT that is
    // unknown, often only because the socket was still connecting at page
    // load, is reported as "Terminal connection: unavailable", never as slow.
    receivedCount < expectedCount ||
    (maxLagMs !== null && maxLagMs >= 3000)
  ) {
    verdict = "slow";
  }

  return {
    verdict,
    first_event_ms: firstEventMs,
    max_lag_ms: maxLagMs,
    received_count: receivedCount,
    expected_count: expectedCount,
    clumped,
    ws_rtt_ms: wsRttMs,
    http_rtt_ms: httpRttMs,
    browser_family: detectBrowserFamily(),
  };
}

export function formatNetworkSummary(result: NetworkCheckResult | null): {
  liveUpdates: string;
  terminalConnection: string;
  checkedTime: string;
} {
  if (!result) {
    return {
      liveUpdates: "Checking…",
      terminalConnection: "Checking…",
      checkedTime: "Checking now",
    };
  }
  const lagSec = result.max_lag_ms !== null ? (result.max_lag_ms / 1000).toFixed(1) : "—";
  const liveUpdates =
    result.verdict === "ok"
      ? `Live updates: OK (${lagSec} s)`
      : result.verdict === "slow"
        ? `Live updates: slow (${lagSec} s lag)`
        : `Live updates: held back ${result.max_lag_ms !== null ? Math.round(result.max_lag_ms / 1000) : "?"} s`;
  const terminalConnection =
    result.ws_rtt_ms !== null
      ? `Terminal connection: OK (${result.ws_rtt_ms} ms)`
      : "Terminal connection: unavailable";
  let checkedTime = "Checked just now";
  if (result.checked_at) {
    try {
      const date = new Date(result.checked_at);
      if (!Number.isNaN(date.getTime())) {
        checkedTime = `Checked ${date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
      }
    } catch {
      /* keep fallback */
    }
  }
  return { liveUpdates, terminalConnection, checkedTime };
}

export async function runNetworkCheck(options?: { fast?: boolean }): Promise<NetworkCheckResult> {
  const nowMs = () => (typeof performance !== "undefined" ? performance.now() : Date.now());

  // 1. HTTP ping RTT
  let httpRttMs: number | null = null;
  try {
    const t0 = nowMs();
    const pingResp = await fetch("/api/network-check/ping", { cache: "no-store" });
    if (pingResp.ok) {
      httpRttMs = Math.max(0, Math.round(nowMs() - t0));
    }
  } catch {
    httpRttMs = null;
  }

  // 2. WebSocket ping RTT (in parallel with stream)
  const wsPromise = pingAppSocket(10000);

  // 3. SSE stream via fetch + ReadableStream reader
  const samples: NetworkSample[] = [];
  const streamUrl = options?.fast ? "/api/network-check/stream?fast=true" : "/api/network-check/stream";
  const streamStarted = nowMs();
  try {
    const response = await fetch(streamUrl, {
      headers: { Accept: "text/event-stream" },
      cache: "no-store",
    });
    if (response.ok && response.body) {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        const arrivedAt = nowMs() - streamStarted;
        buffer += decoder.decode(value, { stream: true });
        let boundary = buffer.indexOf("\n\n");
        while (boundary !== -1) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          for (const line of frame.split("\n")) {
            if (line.startsWith("data:")) {
              try {
                const parsed = JSON.parse(line.slice(5).trim()) as {
                  seq?: number;
                  server_elapsed_ms?: number;
                  flush?: boolean;
                };
                if (typeof parsed.seq === "number" && typeof parsed.server_elapsed_ms === "number") {
                  samples.push({
                    seq: parsed.seq,
                    server_elapsed_ms: parsed.server_elapsed_ms,
                    client_elapsed_ms: arrivedAt,
                    flush: Boolean(parsed.flush),
                  });
                }
              } catch {
                /* ignore malformed SSE frame */
              }
            }
          }
          boundary = buffer.indexOf("\n\n");
        }
      }
    }
  } catch {
    /* network stream failed — evaluated below */
  }

  const wsRttMs = await wsPromise;
  const evaluated = evaluateSamples(samples, EXPECTED_EVENT_COUNT, wsRttMs, httpRttMs);
  const fallback: NetworkCheckResult = {
    ...evaluated,
    checked_at: new Date().toISOString(),
  };

  try {
    const saved = await fetch("/api/network-check/result", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(evaluated),
    });
    if (saved.ok) {
      return (await saved.json()) as NetworkCheckResult;
    }
  } catch {
    /* keep local evaluation if POST fails */
  }
  return fallback;
}
