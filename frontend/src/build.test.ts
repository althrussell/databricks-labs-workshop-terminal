import assert from "node:assert/strict";
import test from "node:test";
import { availableActions, phaseLabel, reviewLabel, type BuildView } from "./build";

const base: BuildView = {
  project: "outages", goal: "Track outages", phase: "build", path: "rapid", status: "working",
  next_decision: "deploy, then inspect", open_questions: [], review: { state: "none" },
  deploy: { state: "not_deployed", preview_url: null }, what_changed: [], requests: [],
  session: { active: false }, updated_at: null,
};

test("only offers what can really happen", () => {
  assert.deepEqual(availableActions(base), { tryApp: false, changeDirection: true, explain: true, stop: false,
    confirmDirection: false, choosePath: false, exportCode: true });
  const live = availableActions({ ...base, deploy: { state: "deployed", preview_url: "https://a.databricksapps.com" },
    session: { active: true, agent: "claude", label: "Claude Code" } });
  assert.equal(live.tryApp, true); assert.equal(live.stop, true);
  const guided = availableActions({ ...base, path: "guided", direction: { text: "Queue", confirmed_by: null } });
  assert.equal(guided.confirmDirection, true);
  assert.equal(availableActions({ ...base, project: null, path: null }).exportCode, false);
});

test("review status never reads as checked when it was not", () => {
  assert.equal(reviewLabel({ state: "none" }), "Not checked yet");
  assert.equal(reviewLabel({ state: "unverified", gaps: ["no browser on this terminal"] }),
    "Not verified: no browser on this terminal");
  assert.equal(reviewLabel({ state: "stale" }), "Changed since it was last checked");
  assert.equal(reviewLabel({ state: "verified", write_proven: true }), "Checked on screen, saving proven");
  assert.equal(phaseLabel("feedback"), "Your turn to try it");
  assert.equal(phaseLabel("mystery"), "Getting started");
});
