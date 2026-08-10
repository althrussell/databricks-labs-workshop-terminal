export interface ShellLink {
  label: string;
  url: string;
  icon: string;
  highlight?: boolean;
}

export interface WorkspaceLink {
  label: string;
  path: string;
  icon: string;
  description: string;
}

export interface IdeaPrompt {
  label: string;
  prompt: string;
}

export type Persona = "technical" | "business";

export interface CredentialStatus {
  configured: boolean;
  rotating: boolean;
  healthy: boolean;
  degraded: boolean;
  state: "unknown" | "rotating" | "degraded" | "unhealthy";
  source: "app_identity_oauth" | "emergency_workshop_pat" | "unknown";
  token_expires_in: number | null;
  last_successful_at: number | null;
  last_error: string | null;
}

export interface AppConfig {
  user: { email: string; is_admin: boolean };
  branding: {
    brand_name: string;
    brand_logo_url: string;
    brand_primary_color: string;
    event_name: string;
    cobranded: boolean;
  };
  workspace_url: string;
  shell: {
    links: ShellLink[];
    workspace_links: WorkspaceLink[];
    features: Record<string, boolean>;
  };
  phase: string;
  broadcast: Broadcast | null;
  limits: { max_sessions_per_user: number };
  credential: CredentialStatus;
  /** This attendee's browser-bound Databricks sign-in, as the server last saw it. */
  obo: OboStatus;
  help: HelpState;
  omnigent_remote: {
    enabled: boolean;
    url: string;
  };
}

export interface AgentInfo {
  id: string;
  label: string;
  description: string;
  icon: string;
  order: number;
  ready: boolean;
  /** Why this agent is not launchable despite being installed ("" when it is). */
  blocked?: string;
  /** Which install step failed terminally, when the agent will never go ready. */
  install_error?: string;
  needs_credentials: boolean;
}

export interface OboStatus {
  enabled: boolean;
  present: boolean;
  fresh: boolean;
  expires_in: number | null;
  last_refresh: number | null;
}

/** One attendee's standing on the Omnigent plane, as the operator panel shows it. */
export interface OmnigentAttendee {
  email: string;
  obo: OboStatus;
  host: { status: string; last_exit_code?: number };
}

export interface OmnigentTier {
  /** False once an operator has demoted the tier fleet-wide. */
  enabled: boolean;
  /** Whether this deployment is wired to a remote Omnigent app at all. */
  remote: boolean;
  attendees: OmnigentAttendee[];
}

export interface RecoveryResult {
  email: string;
  attempted: boolean;
  reason?: string;
  actions?: string[];
  credential_fresh?: boolean;
  obo: OboStatus;
}

export interface SessionInfo {
  id: string;
  agent_id: string;
  label: string;
  created_at: number;
  last_activity: number;
  exited: boolean;
}

export interface PriorSessionInfo extends SessionInfo {
  exited: true;
  exit_reason: string;
}

export interface SessionPayload {
  sessions: SessionInfo[];
  prior_sessions?: PriorSessionInfo[];
}

export function splitSessionPayload(payload: SessionPayload): {
  live: SessionInfo[];
  prior: PriorSessionInfo[];
} {
  const prior = payload.prior_sessions ?? [];
  const priorIds = new Set(prior.map((session) => session.id));
  return {
    live: payload.sessions.filter(
      (session) => !session.exited && !priorIds.has(session.id)
    ),
    prior,
  };
}

export async function relaunchAndAcknowledge(
  prior: PriorSessionInfo,
  create: (agentId: string) => Promise<SessionInfo | null>,
  acknowledge: (priorId: string) => Promise<unknown>
): Promise<SessionInfo | null> {
  // Consume first: if the response is lost, no replacement has been created,
  // so retrying or using the normal launch action cannot duplicate a session.
  await acknowledge(prior.id);
  return create(prior.agent_id);
}

export interface Nugget {
  id: string;
  title: string;
  markdown: string;
  link: { url: string; label: string } | null;
  tags: string[];
  pinned: boolean;
  cta: string | null;
  prompt: string | null;
  matched_topic: string | null;
  nudge: boolean;
}

/** One agent-elicited discovery record, as its subject sees it.
 *
 * Every field but the identifiers is optional, because a record is built from
 * whatever the attendee happened to say — see `server/discovery.py`.
 */
export interface DiscoveryRecord {
  record_id: string;
  captured_at: string;
  agent?: string;
  confidence?: string;
  use_case_title?: string;
  use_case_summary?: string;
  goal?: string;
  timeline?: string;
  industry?: string;
  current_stack?: string[];
  databricks_products?: string[];
  blockers?: string[];
  interest_signals?: string[];
  redactions?: number;
}

export interface Broadcast {
  message: string;
  level: "info" | "success" | "warning" | "error";
  ttl_s: number;
  /** Only banners are retained server-side, so only banners arrive here. */
  surface?: "banner" | "toast";
  durability?: "transient" | "sticky" | "critical";
  clear?: boolean;
}

export interface HelpState {
  raised: boolean;
  note?: string | null;
  raised_at?: number | null;
  message_count?: number;
  help_request_id?: string | null;
}

export interface HelpMessage {
  message_id: string;
  help_request_id?: string | null;
  sender_role: "attendee" | "operator" | string;
  sender?: string;
  body: string;
  created_at: string;
}

export interface HelpThread {
  raised: boolean;
  note?: string | null;
  help_request_id?: string | null;
  messages: HelpMessage[];
}

export interface PresenceUser {
  email: string;
  online: boolean;
  last_seen: number;
  first_seen: number;
  cli_ready: boolean;
  sessions: SessionInfo[];
}

class ApiError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(resp.status, detail);
  }
  return resp.json();
}

export const api = {
  config: () => request<AppConfig>("/api/config"),
  setupStatus: () =>
    request<{
      steps: Record<string, { status: string; error: string | null }>;
      ready: Record<string, boolean>;
      installing: boolean;
    }>("/api/setup-status"),
  agents: () =>
    request<{ agents: AgentInfo[]; credential: CredentialStatus }>("/api/agents"),
  sessions: () => request<SessionPayload>("/api/sessions"),
  createSession: (agentId: string) =>
    request<{ session: SessionInfo }>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ agent_id: agentId }),
    }),
  closeSession: (id: string) => request(`/api/sessions/${id}`, { method: "DELETE" }),
  ackPriorSession: (id: string) =>
    request(`/api/sessions/prior/${encodeURIComponent(id)}`, { method: "DELETE" }),
  typeIntoSession: (id: string, text: string) =>
    request(`/api/sessions/${id}/type`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  recover: () =>
    request<{ recovered: boolean; actions: string[] }>("/api/recover", {
      method: "POST",
    }),
  setPersona: (persona: Persona) =>
    request<{ persona: Persona }>("/api/persona", {
      method: "POST",
      body: JSON.stringify({ persona }),
    }),
  nuggets: () =>
    request<{ phase: string; nuggets: Nugget[]; prompts: IdeaPrompt[] }>("/api/nuggets"),
  myDiscovery: () =>
    request<{ enabled: boolean; records: DiscoveryRecord[] }>("/api/discovery"),
  withdrawDiscovery: (recordId: string) =>
    request<{ redacted: boolean; record_id: string }>("/api/discovery/redact", {
      method: "POST",
      body: JSON.stringify({ record_id: recordId }),
    }),
  adminState: () =>
    request<{ phase: string; phases: string[]; nugget_count: number }>("/api/admin/state"),
  adminPresence: () =>
    request<{ users: PresenceUser[]; session_count: number; credential: CredentialStatus }>(
      "/api/admin/presence"
    ),
  adminPhase: (phase: string) =>
    request("/api/admin/phase", { method: "POST", body: JSON.stringify({ phase }) }),
  adminBroadcast: (message: string, level: string, ttl_s: number) =>
    request("/api/admin/broadcast", {
      method: "POST",
      body: JSON.stringify({ message, level, ttl_s }),
    }),
  adminOmnigentTier: () => request<OmnigentTier>("/api/admin/omnigent-tier"),
  /** Withdraw the Omnigent cards fleet-wide, or put them back. */
  adminSetOmnigentTier: (enabled: boolean) =>
    request<{ enabled: boolean }>("/api/admin/omnigent-tier", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),
  /** Re-mirror, wake the host and nudge the tab — for one attendee or all. */
  adminRecover: (email?: string) =>
    request<{ recovered: string[]; results: RecoveryResult[] }>("/api/admin/recover", {
      method: "POST",
      body: JSON.stringify({ email: email ?? "" }),
    }),
  raiseHelp: (note?: string) =>
    request<{ raised: boolean; pushed: boolean; note?: string | null }>(
      "/api/help/raise",
      { method: "POST", body: JSON.stringify({ note: note ?? "" }) },
    ),
  lowerHelp: () =>
    request<{ raised: boolean; pushed: boolean }>("/api/help/lower", {
      method: "POST",
      body: "{}",
    }),
  helpThread: () => request<HelpThread>("/api/help/thread"),
  postHelpMessage: (body: string) =>
    request<{ message: HelpMessage; pushed: boolean; raised: boolean }>(
      "/api/help/messages",
      { method: "POST", body: JSON.stringify({ body }) },
    ),
  /** Read receipt: this attendee has been shown an operator message. */
  ackHelpMessage: (messageId: string) =>
    request<{ acked: boolean }>("/api/help/ack", {
      method: "POST",
      body: JSON.stringify({ message_id: messageId }),
    }),
};

export { ApiError };
