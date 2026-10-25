import { test } from "node:test";
import assert from "node:assert/strict";
import { changesFrom, choicesFrom } from "./preferences.ts";
import type { AttendeeProfile } from "./api.ts";

function profile(extra: Partial<AttendeeProfile> = {}): AttendeeProfile {
  return { schema_version: 1, revision: 3, help_preference: "", source: "default", legacy_persona: "", ...extra };
}

test("an unchosen approach shows the gentle default and nothing is sent unchanged", () => {
  const saved = profile();
  assert.deepEqual(choicesFrom(saved), { approach: "co_build", detail: "", vocabulary: "" });
  assert.deepEqual(changesFrom(saved, choicesFrom(saved)), { expected_revision: 3 });
});

test("legacy concise becomes brief detail and is migrated on the next save", () => {
  const saved = profile({ help_preference: "concise" });
  assert.equal(choicesFrom(saved).detail, "brief");
  const body = changesFrom(saved, { ...choicesFrom(saved), approach: "learn" });
  assert.deepEqual(body, { expected_revision: 3, working_approach: "learn", explanation_detail: "brief", help_preference: "" });
});

test("each dimension changes independently", () => {
  const saved = profile({ help_preference: "technical", working_approach: "lead", explanation_detail: "detailed" });
  const body = changesFrom(saved, { ...choicesFrom(saved), detail: "brief" });
  assert.deepEqual(body, { expected_revision: 3, explanation_detail: "brief" });
  assert.equal(choicesFrom(saved).vocabulary, "technical");
});
