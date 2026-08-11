import assert from "node:assert/strict";
import test from "node:test";

import { friendlyError, stripDeadEnds } from "./errors";

test("the harness codes an attendee cannot act on become sentences they can", () => {
  for (const raw of [
    "Terminal failed to start: native_terminal_start_failed (see runner logs)",
    "spec_resolver_failed",
  ]) {
    const friendly = friendlyError(raw);
    assert.match(friendly.message, /Databricks sign-in/);
    assert.equal(friendly.action, "recover");
    assert.equal(friendly.actionLabel, "Recover");
    // The fallbacks are the point: the attendee is not blocked, only Omnigent is.
    assert.match(friendly.message, /Claude, Codex and Terminal/);
  }
});

test("nothing ever points the attendee at logs they cannot reach", () => {
  const messages = [
    "Terminal failed to start: native_terminal_start_failed (see runner logs)",
    "spec_resolver_failed — check the logs",
    "runner_disconnected (see the runner log)",
    "Something odd happened, see runner logs",
  ].map((raw) => friendlyError(raw).message.toLowerCase());

  for (const message of messages) {
    assert.ok(!message.includes("runner log"), message);
    assert.ok(!message.includes("check the log"), message);
  }
});

test("stripping dead ends leaves the useful half of a message", () => {
  assert.equal(
    stripDeadEnds("Could not reach the workspace (see runner logs)"),
    "Could not reach the workspace"
  );
  assert.equal(stripDeadEnds("Nothing to strip here"), "Nothing to strip here");
});

test("a still-installing agent offers no button, because waiting is the fix", () => {
  const friendly = friendlyError("Omnigent is still installing — try again in a moment");

  assert.equal(friendly.code, "install_incomplete");
  assert.equal(friendly.action, "none");
  assert.match(friendly.message, /still being installed/);
});

test("a lost connection asks for a reload rather than a credential repair", () => {
  const friendly = friendlyError("websocket_lost", "websocket_lost");

  assert.equal(friendly.action, "reload");
  assert.equal(friendly.actionLabel, "Reload");
});

test("an unmapped server message is passed through, an unmapped code is not", () => {
  assert.equal(
    friendlyError("Maximum sessions reached for this attendee").message,
    "Maximum sessions reached for this attendee"
  );
  // A bare code is not a sentence; showing it is what this module exists to stop.
  assert.match(
    friendlyError("session_start_failed").message,
    /Something went wrong starting that agent/
  );
});

test("the operator's code survives the rewrite", () => {
  // Both sides of the glass must still be joinable on one string.
  assert.equal(
    friendlyError("Terminal failed to start: native_terminal_start_failed").code,
    "native_terminal_start_failed"
  );
  assert.equal(
    friendlyError("boom", "session_start_failed").raw,
    "boom"
  );
});
