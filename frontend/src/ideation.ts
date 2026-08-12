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
 * Launchable first, then Claude by preference, then catalogue order. Readiness
 * has to come before preference: the catalogue leads with Omnigent, whose launch
 * is refused while it installs and again whenever the attendee's sign-in has
 * gone stale, and refusing a prompt on that basis while Codex sits ready beside
 * it is the same dead end as naming an agent the operator never offered.
 *
 * When nothing is launchable yet we still return the agent we would have picked,
 * so the attendee reads "still installing" rather than watching a chip do
 * nothing.
 */
export function ideaAgentId(agents: readonly AgentInfo[]): string {
  const coding = agents.filter((agent) => agent.id !== "bash");
  const preferred = (choices: readonly AgentInfo[]) =>
    choices.find((agent) => agent.id === "claude") ?? choices[0];
  const launchable = coding.filter((agent) => agent.ready && !agent.blocked);
  return (preferred(launchable) ?? preferred(coding))?.id ?? "";
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
