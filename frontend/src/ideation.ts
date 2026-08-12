/** Which agent an ideation prompt should land in.
 *
 * The chips ("Need an idea?", the nugget prompts, "Start me off") type a first
 * sentence at an agent's prompt, so they need a coding agent — but not a
 * particular one. This used to open Claude by name, which broke the moment an
 * operator created a workshop without it: the catalogue no longer offered it,
 * the launch was refused, and the attendee got an error where they expected the
 * sentence they had just clicked.
 */

import type { AgentInfo } from "./api";

/** Live sessions, oldest first, as App holds them. */
export interface SessionLike {
  id: string;
  agent_id: string;
  exited?: boolean;
}

/** The agent to open for an ideation prompt, or "" if this workshop offers none.
 *
 * Claude first when it is offered, because the prompts are written in its voice
 * and it is the cheapest of the three to start. Otherwise the catalogue's own
 * order, which is the order the operator's selection is presented in.
 */
export function ideaAgentId(agents: readonly AgentInfo[]): string {
  const coding = agents.filter((agent) => agent.id !== "bash");
  const preferred = coding.find((agent) => agent.id === "claude") ?? coding[0];
  return preferred?.id ?? "";
}

/** The open session an ideation prompt should be typed into, if there is one.
 *
 * Prefers the agent we would have launched, then any other coding session, then
 * a plain terminal — an attendee who has one thing open wants the text there
 * rather than a second tab appearing beside it.
 */
export function ideaSession<T extends SessionLike>(
  sessions: readonly T[],
  agentId: string
): T | undefined {
  const live = sessions.filter((session) => !session.exited);
  return (
    live.find((session) => session.agent_id === agentId) ??
    live.find((session) => session.agent_id !== "bash") ??
    live[0]
  );
}
