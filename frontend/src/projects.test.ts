import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, type ProjectInventory } from "./api";
import { recoverWithConfirmation } from "./projects";

const complete: ProjectInventory = { active: "film-club", projects: [
  {name: "film-club", goal: "Choose our next film", available: true, saved: true, missing: false},
] };

test("lost response confirms completed recovery without submitting it again", async () => {
  let writes = 0; let reads = 0;
  const result = await recoverWithConfirmation("film-club", "", async () => {
    ++writes; throw new TypeError("Failed to fetch");
  }, async () => { ++reads; return complete; });
  assert.equal(result, complete); assert.equal(writes, 1); assert.equal(reads, 1);
});

test("unavailable or unselected source never turns a lost response into success", async () => {
  for (const state of [
    {...complete, active: "club-agenda"},
    {...complete, projects: []},
    {...complete, projects: complete.projects.map(p => ({...p, available: false}))},
    {...complete, projects: complete.projects.map(p => ({...p, missing: true}))},
  ]) {
    const lost = new TypeError("Failed to fetch");
    await assert.rejects(recoverWithConfirmation("film-club", "", async () => {throw lost;}, async () => state), error => error === lost);
  }
});

test("an explicit setup failure or a previously selected build stays unconfirmed", async () => {
  for (const [before, error] of [["", new ApiError(400, "Source recovered, but project setup is incomplete")], ["film-club", new TypeError("Failed to fetch")]] as const) {
    let reads = 0;
    await assert.rejects(recoverWithConfirmation("film-club", before, async () => {throw error;}, async () => {++reads; return complete;}), caught => caught === error);
    assert.equal(reads, 0);
  }
});

test("a failed confirmation read retains the original error", async () => {
  const lost = new TypeError("Failed to fetch");
  await assert.rejects(recoverWithConfirmation("film-club", "", async () => {throw lost;}, async () => {throw new TypeError("Unavailable");}), caught => caught === lost);
});

test("after a restart the attendee is told what came back and what did not", async () => {
  const { restoreNotice, restoreInProgress } = await import("./projects");
  const at = (seconds: number) => `t${seconds}`;
  const base = { id: "1.000", restored: [], failed: [], active: "", saved_at: null };
  assert.equal(restoreNotice({ ...base, state: "checking" }, at), "");
  assert.equal(restoreInProgress({ ...base, state: "checking" }), true);
  assert.match(restoreNotice({ ...base, state: "restoring" }, at), /Restoring your saved builds/);
  assert.equal(
    restoreNotice({ ...base, state: "restored", restored: ["substation-outages", "repair-desk"], active: "substation-outages", saved_at: 42 }, at),
    "Your terminal restarted. We restored substation-outages from your last saved commit (t42). " +
      "Changes after that commit aren't included, and the agent's earlier conversation isn't either. " +
      "repair-desk is back in My builds too.");
  assert.match(restoreNotice({ ...base, state: "restored", restored: ["repair-desk"] }, at),
    /We restored repair-desk from your last saved commit; open My builds to continue/);
  const partial = restoreNotice({ ...base, state: "partial", restored: ["repair-desk"], failed: ["garden-plan"], active: "repair-desk", saved_at: 7 }, at);
  assert.match(partial, /We restored repair-desk from your last saved commit \(t7\)/);
  assert.match(partial, /We couldn't restore garden-plan automatically\. Open My builds and choose Recover source; nothing on this terminal was overwritten\./);
  assert.equal(restoreNotice({ ...base, state: "failed", failed: ["a", "b"] }, at),
    "Your terminal restarted. We couldn't restore a and b automatically. Open My builds and choose Recover source; nothing on this terminal was overwritten.");
  assert.equal(restoreInProgress({ ...base, state: "restored" }), false);
  assert.equal(restoreNotice(null, at), "");
});
