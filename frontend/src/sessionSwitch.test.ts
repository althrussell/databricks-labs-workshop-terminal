import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, type SessionInfo } from "./api.ts";
import {
  ActiveSessionChanged,
  agentSelection,
  closeThenCreate,
  resolveSessionConflict,
} from "./sessionSwitch.ts";

function session(id: string, agent_id: string, label = agent_id): SessionInfo {
  return { id, agent_id, label, created_at: 1, last_activity: 1, exited: false };
}

test("the active agent focuses while another agent requires confirmation", () => {
  const active = session("one", "claude", "Claude Code");
  assert.equal(agentSelection(active, "claude"), "focus");
  assert.equal(agentSelection(active, "codex"), "confirm");
  assert.equal(agentSelection(null, "codex"), "launch");
});

test("a same-agent conflict returns the live session so its prompt is not dropped", () => {
  const active = session("one", "claude", "Claude Code");
  assert.deepEqual(
    resolveSessionConflict(active, "claude", "Build a dashboard"),
    { action: "focus", active }
  );
});

test("a cross-agent conflict carries the prompt into confirmation", () => {
  const active = session("one", "claude", "Claude Code");
  assert.deepEqual(
    resolveSessionConflict(active, "codex", "Build a dashboard"),
    {
      action: "confirm",
      active,
      requestedAgentId: "codex",
      starterPrompt: "Build a dashboard",
    }
  );
});

test("a conflict whose active session just ended is safe to retry", () => {
  assert.deepEqual(
    resolveSessionConflict(null, "codex", "Build a dashboard"),
    { action: "missing" }
  );
});

test("a confirmed switch closes, refreshes, then creates", async () => {
  const calls: string[] = [];
  const created = await closeThenCreate(session("one", "claude"), "codex", {
    close: async (id) => { calls.push(`close:${id}`); },
    refresh: async () => { calls.push("refresh"); return null; },
    create: async (id) => { calls.push(`create:${id}`); return session("two", id); },
  });

  assert.equal(created.agent_id, "codex");
  assert.deepEqual(calls, ["close:one", "refresh", "create:codex"]);
});

test("a close already completed by another tab can continue", async () => {
  const created = await closeThenCreate(session("one", "claude"), "codex", {
    close: async () => { throw new ApiError(404, "gone"); },
    refresh: async () => null,
    create: async (id) => session("two", id),
  });
  assert.equal(created.agent_id, "codex");
});

test("a newly active session is never closed without another confirmation", async () => {
  let creates = 0;
  const other = session("other", "omnigent", "Omnigent");
  await assert.rejects(
    closeThenCreate(session("one", "claude"), "codex", {
      close: async () => undefined,
      refresh: async () => other,
      create: async (id) => { creates += 1; return session("two", id); },
    }),
    (error) => error instanceof ActiveSessionChanged && error.active === other
  );
  assert.equal(creates, 0);
});

test("a replacement create failure is returned after the slot is proven empty", async () => {
  await assert.rejects(
    closeThenCreate(session("one", "claude"), "codex", {
      close: async () => undefined,
      refresh: async () => null,
      create: async () => { throw new Error("Codex failed to start"); },
    }),
    /Codex failed to start/
  );
});
