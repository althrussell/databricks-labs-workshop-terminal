import assert from "node:assert/strict";
import { test } from "node:test";
import * as apiModule from "./api.ts";

test("restart ghosts remain separate from websocket-attachable live sessions", () => {
  const result = apiModule.splitSessionPayload({
    sessions: [
      {
        id: "live",
        agent_id: "bash",
        label: "Terminal",
        created_at: 1,
        last_activity: 2,
        exited: false,
      },
    ],
    prior_sessions: [
      {
        id: "ghost",
        agent_id: "claude",
        label: "Claude",
        created_at: 1,
        last_activity: 2,
        exited: true,
        exit_reason: "server_restarted",
      },
    ],
  });

  assert.deepEqual(result.live.map((session) => session.id), ["live"]);
  assert.deepEqual(result.prior.map((session) => session.id), ["ghost"]);
  assert.equal(result.prior[0].exit_reason, "server_restarted");
});

test("a ghost id can never enter the live TerminalView session list", () => {
  const result = apiModule.splitSessionPayload({
    sessions: [
      {
        id: "overlap",
        agent_id: "bash",
        label: "Terminal",
        created_at: 1,
        last_activity: 2,
        exited: false,
      },
    ],
    prior_sessions: [
      {
        id: "overlap",
        agent_id: "bash",
        label: "Terminal",
        created_at: 1,
        last_activity: 2,
        exited: true,
        exit_reason: "server_restarted",
      },
    ],
  });

  assert.deepEqual(result.live, []);
  assert.deepEqual(result.prior.map((session) => session.id), ["overlap"]);
});

test("successful ghost relaunch consumes it before creating the replacement", async () => {
  assert.equal(typeof apiModule.relaunchAndAcknowledge, "function");
  const calls: string[] = [];
  const prior = {
    id: "ghost",
    agent_id: "claude",
    label: "Claude",
    created_at: 1,
    last_activity: 2,
    exited: true as const,
    exit_reason: "server_restarted",
  };

  const session = await apiModule.relaunchAndAcknowledge(
    prior,
    async (agentId) => {
      calls.push(`create:${agentId}`);
      return {
        id: "live",
        agent_id: agentId,
        label: "Claude",
        created_at: 3,
        last_activity: 3,
        exited: false,
      };
    },
    async (id) => {
      calls.push(`ack:${id}`);
    }
  );

  assert.equal(session?.id, "live");
  assert.deepEqual(calls, ["ack:ghost", "create:claude"]);
});

test("lost ghost acknowledgement response never creates a duplicate replacement", async () => {
  const calls: string[] = [];
  const prior = {
    id: "ghost",
    agent_id: "claude",
    label: "Claude",
    created_at: 1,
    last_activity: 2,
    exited: true as const,
    exit_reason: "server_restarted",
  };

  await assert.rejects(
    apiModule.relaunchAndAcknowledge(
      prior,
      async () => {
        calls.push("create");
        return null;
      },
      async () => {
        calls.push("ack");
        throw new Error("response lost");
      }
    ),
    /response lost/
  );

  assert.deepEqual(calls, ["ack"]);
});

test("launch failure happens only after ghost consumption", async () => {
  const calls: string[] = [];
  const prior = {
    id: "ghost",
    agent_id: "claude",
    label: "Claude",
    created_at: 1,
    last_activity: 2,
    exited: true as const,
    exit_reason: "server_restarted",
  };

  const session = await apiModule.relaunchAndAcknowledge(
    prior,
    async () => {
      calls.push("create");
      return null;
    },
    async () => {
      calls.push("ack");
    }
  );

  assert.equal(session, null);
  assert.deepEqual(calls, ["ack", "create"]);
});

test("prior-session acknowledgement uses the owner-scoped delete API", async () => {
  const originalFetch = globalThis.fetch;
  let request: { input: string; init?: RequestInit } | undefined;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    request = { input: String(input), init };
    return new Response(JSON.stringify({ status: "ok" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
  try {
    await apiModule.api.ackPriorSession("ghost/id");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(request?.input, "/api/sessions/prior/ghost%2Fid");
  assert.equal(request?.init?.method, "DELETE");
});
