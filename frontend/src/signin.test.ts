import assert from "node:assert/strict";
import test from "node:test";

import type { OboStatus } from "./api";
import { signInNotice } from "./signin";

function obo(overrides: Partial<OboStatus> = {}): OboStatus {
  return {
    enabled: true,
    present: true,
    fresh: true,
    expires_in: 3600,
    last_refresh: null,
    ...overrides,
  };
}

test("the rule is on screen the whole time it applies", () => {
  const state = signInNotice(true, obo());
  assert.equal(state.kind, "rule");
});

test("a stale sign-in is escalated rather than left as a quiet note", () => {
  assert.equal(signInNotice(true, obo({ fresh: false })).kind, "expired");
});

test("a sign-in with no time left is expired whatever the freshness flag says", () => {
  assert.equal(signInNotice(true, obo({ expires_in: 0 })).kind, "expired");
});

test("an attendee who has not signed in yet is not told they expired", () => {
  // The normal state for the first second of an event. Claiming expiry there
  // reads as a broken product before anyone has done anything.
  const state = signInNotice(true, obo({ present: false, fresh: false, expires_in: null }));
  assert.equal(state.kind, "rule");
});

test("a deployment with no Omnigent plane says nothing at all", () => {
  // Closing the tab costs nothing there, and a warning about an impossible
  // consequence teaches attendees to ignore the strip that matters.
  assert.equal(signInNotice(false, obo()).kind, "none");
  assert.equal(signInNotice(true, obo({ enabled: false })).kind, "none");
  assert.equal(signInNotice(true, undefined).kind, "none");
});

test("a sign-in close to renewal still reads as the plain rule", () => {
  // Renewal happens on its own and needs nothing from the attendee, so the
  // last minutes of a token are not an event worth escalating.
  assert.equal(signInNotice(true, obo({ expires_in: 60 })).kind, "rule");
});
