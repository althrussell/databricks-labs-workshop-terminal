import assert from "node:assert/strict";
import test from "node:test";
import { announceWhileAway, flashTitle, type AttentionSurface } from "./attention";
import { agentToResumeInNewBuild } from "./projects";
import { isBadGateway, retryQuietly } from "./transientRetry";

function surface(hidden: boolean, title = "Workshop Terminal") {
  const titles: string[] = [];
  const visible: Array<() => void> = [];
  const notes: Array<[string, string]> = [];
  const value: AttentionSurface & { titles: string[]; notes: Array<[string, string]>; show(): void } = {
    hidden,
    title,
    titles,
    notes,
    setTitle(next: string) { titles.push(next); },
    onVisible(listener: () => void) { visible.push(listener); return () => undefined; },
    notify(heading: string, body: string) { notes.push([heading, body]); },
    show() { value.hidden = false; visible.forEach((fn) => fn()); },
  };
  return value;
}

function timers() {
  let next = 1; const live = new Map<number, () => void>();
  return {
    schedule: (fn: () => void) => { const id = next++; live.set(id, fn); return id; },
    cancel: (id: unknown) => { live.delete(id as number); },
    fire: () => { for (const fn of [...live.values()]) fn(); },
    count: () => live.size,
  };
}

test("a reminder while the page is hidden flashes the title and raises a notification", () => {
  const page = surface(true);
  const clock = timers();
  announceWhileAway(page, "Wrap starts in 30 minutes. Export your code now.", {
    schedule: clock.schedule, cancel: clock.cancel, maxFlashes: 4,
  });
  assert.deepEqual(page.notes, [["Workshop update", "Wrap starts in 30 minutes. Export your code now."]]);
  clock.fire();
  clock.fire();
  assert.deepEqual(page.titles, ["(!) Wrap starts in 30", "Workshop Terminal"]);
});

test("returning to the page stops the flashing and restores the title", () => {
  const page = surface(true);
  const clock = timers();
  announceWhileAway(page, "Wrap starts in 10 minutes", { schedule: clock.schedule, cancel: clock.cancel });
  clock.fire();
  page.show();
  assert.equal(clock.count(), 0);
  assert.equal(page.titles.at(-1), "Workshop Terminal");
});

test("the flash stops after its cap so a forgotten tab is not flashed forever", () => {
  const page = surface(true);
  const clock = timers();
  announceWhileAway(page, "Reminder", { schedule: clock.schedule, cancel: clock.cancel, maxFlashes: 2 });
  for (let i = 0; i < 5; i += 1) clock.fire();
  assert.equal(clock.count(), 0);
  assert.equal(page.titles.at(-1), "Workshop Terminal");
});

test("a visible page is covered by its toast and gets no flash or notification", () => {
  const page = surface(false);
  const clock = timers();
  announceWhileAway(page, "Reminder", { schedule: clock.schedule, cancel: clock.cancel });
  assert.equal(clock.count(), 0);
  assert.deepEqual(page.notes, []);
  assert.deepEqual(page.titles, []);
});

test("an empty message announces nothing", () => {
  const page = surface(true);
  announceWhileAway(page, "   ", { schedule: timers().schedule });
  assert.deepEqual(page.notes, []);
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
