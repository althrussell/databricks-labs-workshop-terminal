import assert from "node:assert/strict";
import test from "node:test";
import { announceWhileAway, flashTitle, stopFlash, type AttentionSurface } from "./attention";
import { agentToResumeInNewBuild } from "./projects";
import { isBadGateway, retryQuietly } from "./transientRetry";

// A page whose title is real state, so a test can see what the attendee would see.
function page(hidden: boolean, title = "Workshop Terminal") {
  const state = { title, hidden };
  const visibleListeners = new Set<() => void>();
  const notes: Array<[string, string]> = [];
  const titles: string[] = [];
  const surface: AttentionSurface = {
    get hidden() { return state.hidden; },
    get title() { return state.title; },
    setTitle(value: string) { state.title = value; titles.push(value); },
    onVisible(listener: () => void) {
      visibleListeners.add(listener);
      return () => { visibleListeners.delete(listener); };
    },
    notify(heading: string, body: string) { notes.push([heading, body]); },
  };
  return {
    surface, state, notes, titles,
    listeners: () => visibleListeners.size,
    show() { state.hidden = false; [...visibleListeners].forEach((fn) => fn()); },
  };
}

// A scheduler the test drives by hand: one live interval at a time.
function clock() {
  let next = 1;
  const live = new Map<number, () => void>();
  return {
    schedule: (fn: () => void) => { const id = next++; live.set(id, fn); return id; },
    cancel: (id: unknown) => { live.delete(id as number); },
    fire: () => { for (const fn of [...live.values()]) fn(); },
    live: () => live.size,
  };
}

function reset() { stopFlash(); }

test("a reminder while the page is hidden flashes the title and raises a notification", () => {
  reset();
  const tab = page(true);
  const timer = clock();
  announceWhileAway(tab.surface, "Wrap starts in 30 minutes. Export your code now.", {
    schedule: timer.schedule, cancel: timer.cancel,
  });
  assert.deepEqual(tab.notes, [["Workshop update", "Wrap starts in 30 minutes. Export your code now."]]);
  timer.fire();
  timer.fire();
  assert.deepEqual(tab.titles, ["(!) Wrap starts in 30", "Workshop Terminal"]);
  stopFlash();
});

test("a second broadcast during a flash keeps the original title, not the flashed one", () => {
  reset();
  const tab = page(true);
  const timer = clock();
  const options = { schedule: timer.schedule, cancel: timer.cancel };
  announceWhileAway(tab.surface, "Wrap starts in 30 minutes", options);
  timer.fire(); // title is now the flash
  assert.equal(tab.state.title, "(!) Wrap starts in 30");
  announceWhileAway(tab.surface, "Wrap starts in 10 minutes", options);
  assert.equal(timer.live(), 1, "one interval for the page, not one per message");
  for (let i = 0; i < 4; i += 1) timer.fire();
  tab.show();
  assert.equal(tab.state.title, "Workshop Terminal", "restored to the original, never the flashed label");
  assert.equal(tab.listeners(), 0, "the visibility listener is removed");
});

test("the second message updates the flash label", () => {
  reset();
  const tab = page(true);
  const timer = clock();
  const options = { schedule: timer.schedule, cancel: timer.cancel };
  announceWhileAway(tab.surface, "Wrap starts in 30 minutes", options);
  announceWhileAway(tab.surface, "Export now please", options);
  timer.fire();
  assert.equal(tab.state.title, "(!) Export now please");
  stopFlash();
});

test("returning to the page stops the flashing and restores the title", () => {
  reset();
  const tab = page(true);
  const timer = clock();
  announceWhileAway(tab.surface, "Wrap starts in 10 minutes", { schedule: timer.schedule, cancel: timer.cancel });
  timer.fire();
  tab.show();
  assert.equal(timer.live(), 0);
  assert.equal(tab.state.title, "Workshop Terminal");
});

test("the flash stops after its cap so a forgotten tab is not flashed forever", () => {
  reset();
  const tab = page(true);
  const timer = clock();
  announceWhileAway(tab.surface, "Reminder", { schedule: timer.schedule, cancel: timer.cancel, maxFlashes: 2 });
  for (let i = 0; i < 5; i += 1) timer.fire();
  assert.equal(timer.live(), 0);
  assert.equal(tab.state.title, "Workshop Terminal");
});

test("a visible page is covered by its toast and gets no flash or notification", () => {
  reset();
  const tab = page(false);
  const timer = clock();
  announceWhileAway(tab.surface, "Reminder", { schedule: timer.schedule, cancel: timer.cancel });
  assert.equal(timer.live(), 0);
  assert.deepEqual(tab.notes, []);
  assert.deepEqual(tab.titles, []);
});

test("an empty message announces nothing", () => {
  reset();
  const tab = page(true);
  announceWhileAway(tab.surface, "   ", { schedule: clock().schedule });
  assert.deepEqual(tab.notes, []);
});

test("the flash label is the opening words of the message", () => {
  assert.equal(flashTitle("  Export your   code now please and thanks "), "(!) Export your code now");
});

test("a 502 is retried quietly and the poll succeeds", async () => {
  let calls = 0;
  const waits: number[] = [];
  const value = await retryQuietly(async () => {
    calls += 1;
    if (calls < 3) throw Object.assign(new Error("bad gateway"), { status: 502 });
    return "ok";
  }, { sleep: async (ms) => { waits.push(ms); }, delaysMs: [300, 900] });
  assert.equal(value, "ok");
  assert.equal(calls, 3);
  assert.deepEqual(waits, [300, 900]);
});

test("a 502 that never clears is re-thrown after the last try", async () => {
  let calls = 0;
  await assert.rejects(retryQuietly(async () => {
    calls += 1;
    throw Object.assign(new Error("bad gateway"), { status: 502 });
  }, { attempts: 3, sleep: async () => undefined }), { status: 502 });
  assert.equal(calls, 3);
});

test("other statuses are a real answer and are not retried", async () => {
  let calls = 0;
  await assert.rejects(retryQuietly(async () => {
    calls += 1;
    throw Object.assign(new Error("forbidden"), { status: 403 });
  }, { sleep: async () => undefined }), { status: 403 });
  assert.equal(calls, 1);
  assert.equal(isBadGateway(Object.assign(new Error("x"), { status: 503 })), false);
  assert.equal(isBadGateway(null), false);
});

test("the agent that was stopped to switch resumes in a new build", () => {
  assert.equal(agentToResumeInNewBuild({ new: true }, { agent_id: "claude" }), "claude");
});

test("nothing resumes when nothing was running, on a switch to an existing build, or on a recovery", () => {
  assert.equal(agentToResumeInNewBuild({ new: true }, null), null);
  assert.equal(agentToResumeInNewBuild({ new: false }, { agent_id: "codex" }), null);
  assert.equal(agentToResumeInNewBuild({ new: true, recover: true }, { agent_id: "codex" }), null);
});
