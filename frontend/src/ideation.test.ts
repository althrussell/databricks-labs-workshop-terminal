// Run: node --test src/ideation.test.ts   (Node strips TS types natively)
import { test } from "node:test";
import assert from "node:assert/strict";
import { ideaAgentId, ideaSession, type SessionLike } from "./ideation.ts";
import type { AgentInfo } from "./api.ts";

function agent(id: string): AgentInfo {
  return {
    id,
    label: id,
    description: id,
    icon: id,
    order: 0,
    ready: true,
    needs_credentials: false,
  };
}

function session(id: string, agent_id: string, exited = false): SessionLike {
  return { id, agent_id, exited };
}

test("Claude takes the prompt when the workshop offers it", () => {
  const offered = [agent("omnigent"), agent("claude"), agent("bash")];
  assert.equal(ideaAgentId(offered), "claude");
});

test("a workshop created without Claude still has somewhere to type", () => {
  // The bug this exists for: the chips named Claude, the operator had not
  // offered it, and the launch was refused in front of the attendee.
  assert.equal(ideaAgentId([agent("omnigent"), agent("codex")]), "omnigent");
  assert.equal(ideaAgentId([agent("codex"), agent("bash")]), "codex");
});

test("a plain terminal is never launched to hold a prompt for an agent", () => {
  assert.equal(ideaAgentId([agent("bash")]), "");
  assert.equal(ideaAgentId([]), "");
});

test("an already open session is used rather than a second one opened", () => {
  const sessions = [session("s1", "bash"), session("s2", "codex")];
  assert.equal(ideaSession(sessions, "codex")?.id, "s2");
});

test("a coding session wins over a plain terminal, whichever is open", () => {
  const sessions = [session("s1", "bash"), session("s2", "claude")];
  assert.equal(ideaSession(sessions, "codex")?.id, "s2");
});

test("a terminal is better than nowhere when it is all there is", () => {
  assert.equal(ideaSession([session("s1", "bash")], "claude")?.id, "s1");
});

test("an exited session is not somewhere to type", () => {
  const sessions = [session("s1", "claude", true)];
  assert.equal(ideaSession(sessions, "claude"), undefined);
});
