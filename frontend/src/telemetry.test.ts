import assert from "node:assert/strict";
import test from "node:test";

import { codeFromMessage, postAttendeeError, reportAttendeeError } from "./telemetry";

test("an Omnigent code inside a user-facing message survives into the report", () => {
  assert.equal(
    codeFromMessage(
      "Terminal failed to start: native_terminal_start_failed (see runner logs)",
      "session_start_failed"
    ),
    "native_terminal_start_failed"
  );
  assert.equal(
    codeFromMessage("spec_resolver_failed", "session_start_failed"),
    "spec_resolver_failed"
  );
});

test("prose the attendee sees is mapped onto the same vocabulary", () => {
  assert.equal(
    codeFromMessage("Claude Code is still installing — try again in a moment", "turn_failed"),
    "install_incomplete"
  );
  assert.equal(
    codeFromMessage("No workshop credential available", "turn_failed"),
    "credential_unavailable"
  );
});

test("an unrecognised message falls back to the caller's code", () => {
  assert.equal(
    codeFromMessage("Something nobody predicted", "session_start_failed"),
    "session_start_failed"
  );
});

test("reporting posts the code and never throws when the network is gone", () => {
  const calls: Array<{ url: string; body: unknown }> = [];
  const original = globalThis.fetch;
  globalThis.fetch = ((url: string, init: RequestInit) => {
    calls.push({ url, body: JSON.parse(String(init.body)) });
    return Promise.reject(new Error("offline"));
  }) as unknown as typeof fetch;
  try {
    reportAttendeeError("websocket_lost", "reconnect_exhausted", { agentId: "pi" });
  } finally {
    globalThis.fetch = original;
  }

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/telemetry/error");
  assert.deepEqual(calls[0].body, {
    code: "websocket_lost",
    detail: "reconnect_exhausted",
    agent_id: "pi",
    session_id: "",
  });
});

async function withFetch<T>(
  reply: unknown,
  run: () => Promise<T>
): Promise<T> {
  const original = globalThis.fetch;
  globalThis.fetch = (() =>
    reply instanceof Error
      ? Promise.reject(reply)
      : Promise.resolve({
          ok: true,
          json: () => Promise.resolve(reply),
        })) as unknown as typeof fetch;
  try {
    return await run();
  } finally {
    globalThis.fetch = original;
  }
}

test("the server's repair verdict is what decides whether to try again", async () => {
  // The credential was rewritten while the report was being handled, so the
  // attendee should get a working terminal rather than an error to read.
  assert.equal(
    await withFetch({ status: "ok", retry: true }, () =>
      postAttendeeError("spec_resolver_failed", "auth")
    ),
    true
  );
  // Nothing was repaired: retrying would just fail again in front of them.
  assert.equal(
    await withFetch({ status: "ok", retry: false }, () =>
      postAttendeeError("turn_failed", "model gave up")
    ),
    false
  );
});

test("an unreachable server promises nothing rather than looping", async () => {
  assert.equal(
    await withFetch(new Error("offline"), () =>
      postAttendeeError("session_start_failed")
    ),
    false
  );
});
