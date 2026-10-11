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
