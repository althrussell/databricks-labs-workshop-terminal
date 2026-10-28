import assert from "node:assert/strict";
import test from "node:test";
import { evaluateSamples, formatNetworkSummary } from "./networkCheck";

test("normal SSE stream with low lag and working WS produces ok verdict", () => {
  const samples = [0, 1000, 2000, 3000, 5000, 8000, 12000, 17000, 23000, 30000].map(
    (ms, seq) => ({
      seq,
      server_elapsed_ms: ms,
      client_elapsed_ms: ms + 120,
      flush: false,
    })
  );
  samples.push({
    seq: 10,
    server_elapsed_ms: 30000,
    client_elapsed_ms: 30150,
    flush: true,
  });
  const result = evaluateSamples(samples, 11, 45, 30);
  assert.equal(result.verdict, "ok");
  assert.equal(result.clumped, false);
  assert.equal(result.max_lag_ms, 150);
});

test("SSE stream buffered until 32 KB flush or close produces blocked verdict", () => {
  const samples = [0, 1000, 2000, 3000, 5000, 8000, 12000, 17000, 23000, 30000].map(
    (ms, seq) => ({
      seq,
      server_elapsed_ms: ms,
      client_elapsed_ms: 30100 + seq * 5,
      flush: false,
    })
  );
  samples.push({
    seq: 10,
    server_elapsed_ms: 30000,
    client_elapsed_ms: 30160,
    flush: true,
  });
  const result = evaluateSamples(samples, 11, 60, 40);
  assert.equal(result.verdict, "blocked");
  assert.equal(result.clumped, true);
  assert.ok((result.first_event_ms ?? 0) > 15000);
});

test("SSE stream with moderate 5s lag produces slow verdict", () => {
  const samples = [0, 1000, 2000, 3000, 5000, 8000, 12000, 17000, 23000, 30000].map(
    (ms, seq) => ({
      seq,
      server_elapsed_ms: ms,
      client_elapsed_ms: ms + (seq === 4 ? 5200 : 200),
      flush: false,
    })
  );
  samples.push({
    seq: 10,
    server_elapsed_ms: 30000,
    client_elapsed_ms: 30200,
    flush: true,
  });
  const result = evaluateSamples(samples, 11, 80, 40);
  assert.equal(result.verdict, "slow");
  assert.equal(result.max_lag_ms, 5200);
});

test("formatNetworkSummary renders plain-language status lines", () => {
  const summary = formatNetworkSummary({
    verdict: "blocked",
    checked_at: "2026-10-27T02:03:00Z",
    first_event_ms: 30100,
    max_lag_ms: 30100,
    received_count: 11,
    expected_count: 11,
    clumped: true,
    ws_rtt_ms: 80,
    http_rtt_ms: 40,
    browser_family: "Chrome",
  });
  assert.equal(summary.liveUpdates, "Live updates: held back 30 s");
  assert.equal(summary.terminalConnection, "Terminal connection: OK (80 ms)");
});
