import assert from "node:assert/strict";
import test from "node:test";
import { api, ApiError } from "./api.ts";
import { advanceWorkshopState, onWorkshopClosing, workshopNotice } from "./workshopLifecycle.ts";

test("closing and closed stay disabled despite older in-flight open reads", () => {
  assert.equal(advanceWorkshopState(null, "open"), "open");
  assert.equal(advanceWorkshopState("open", "closing"), "closing");
  assert.equal(advanceWorkshopState("closing", "open"), "closing");
  assert.equal(advanceWorkshopState("closing", "closed"), "closed");
  assert.equal(advanceWorkshopState("closed", "closing"), "closed");
  assert.equal(advanceWorkshopState("closed", "open"), "closed");
});

test("attendee closing copy gives no retry/relaunch or operator capture claims", () => {
  for (const state of ["closing", "closed"] as const) {
    assert.match(workshopNotice(state), /New input is disabled/);
    assert.doesNotMatch(workshopNotice(state), /capture|account team|insight|follow.up|relaunch|retry|preserv|saved/i);
  }
});

test("only a structured close refusal disables the page, without retrying the action", async () => {
  const original = globalThis.fetch;
  let notifications = 0;
  let calls = 0;
  const off = onWorkshopClosing(() => { notifications += 1; });
  const respond = (status: number, detail: unknown) => {
    globalThis.fetch = (async () => {
      calls += 1;
      return new Response(JSON.stringify({ detail }), { status, headers: { "Content-Type": "application/json" } });
    }) as typeof fetch;
  };
  try {
    respond(409, { code: "workshop_closing", message: "This workshop is closing. New input is disabled." });
    await assert.rejects(api.postHelpMessage("already typed"), (error: unknown) => error instanceof ApiError && error.status === 409);
    assert.equal(notifications, 1);
    assert.equal(calls, 1);
    respond(409, { code: "session_conflict", message: "Already running" });
    await assert.rejects(api.createSession("claude"));
    respond(503, "Workshop is temporarily unavailable. Please retry shortly.");
    await assert.rejects(api.workshopStatus());
    assert.equal(notifications, 1);
    off();
    respond(409, { code: "workshop_closing", message: "Closed" });
    await assert.rejects(api.postHelpMessage("late"));
    assert.equal(notifications, 1);
  } finally { off(); globalThis.fetch = original; }
});
