import { ApiError, type SessionInfo } from "./api";

export type AgentSelection = "launch" | "focus" | "confirm";

export type SessionConflictResolution =
  | { action: "missing" }
  | { action: "focus"; active: SessionInfo }
  | {
      action: "confirm";
      active: SessionInfo;
      requestedAgentId: string;
      starterPrompt: string;
    };

export function agentSelection(
  active: SessionInfo | null,
  requestedAgentId: string
): AgentSelection {
  if (!active) return "launch";
  return active.agent_id === requestedAgentId ? "focus" : "confirm";
}

/** Decide what to do after create reports that the one session slot is busy.
 *
 * A same-agent conflict is a successful reconciliation from the attendee's
 * perspective: callers must receive the live session so they can still deliver
 * a starter prompt. A different agent needs confirmation, with that prompt
 * retained until the replacement session is ready.
 */
export function resolveSessionConflict(
  active: SessionInfo | null,
  requestedAgentId: string,
  starterPrompt: string
): SessionConflictResolution {
  if (!active) return { action: "missing" };
  if (active.agent_id === requestedAgentId) {
    return { action: "focus", active };
  }
  return {
    action: "confirm",
    active,
    requestedAgentId,
    starterPrompt,
  };
}

export interface SwitchOperations {
  close: (sessionId: string) => Promise<unknown>;
  refresh: () => Promise<SessionInfo | null>;
  create: (agentId: string) => Promise<SessionInfo>;
}

export class ActiveSessionChanged extends Error {
  active: SessionInfo;

  constructor(active: SessionInfo) {
    super(`${active.label} became active while agents were switching`);
    this.active = active;
  }
}

/** Close the confirmed session, prove the slot is empty, then create.
 *
 * A 404 on close means another tab already closed it and is safe to continue.
 * Any live session observed after refresh is never closed without a new user
 * confirmation: it may have been created by that other tab in the meantime.
 */
export async function closeThenCreate(
  current: SessionInfo,
  requestedAgentId: string,
  operations: SwitchOperations
): Promise<SessionInfo> {
  try {
    await operations.close(current.id);
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 404) throw error;
  }

  const active = await operations.refresh();
  if (active) throw new ActiveSessionChanged(active);
  return operations.create(requestedAgentId);
}
