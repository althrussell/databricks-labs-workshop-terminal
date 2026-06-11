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

export interface CredentialStatus {
  configured: boolean;
  rotating: boolean;
  healthy: boolean;
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
}

export interface AgentInfo {
  id: string;
  label: string;
  description: string;
  icon: string;
  order: number;
  ready: boolean;
  needs_credentials: boolean;
}

export interface SessionInfo {
  id: string;
  agent_id: string;
  label: string;
  created_at: number;
  last_activity: number;
  exited: boolean;
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

export interface Broadcast {
  message: string;
  level: "info" | "success" | "warning";
  ttl_s: number;
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
  sessions: () => request<{ sessions: SessionInfo[] }>("/api/sessions"),
  createSession: (agentId: string) =>
    request<{ session: SessionInfo }>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ agent_id: agentId }),
    }),
  closeSession: (id: string) => request(`/api/sessions/${id}`, { method: "DELETE" }),
  typeIntoSession: (id: string, text: string) =>
    request(`/api/sessions/${id}/type`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  nuggets: () =>
    request<{ phase: string; nuggets: Nugget[]; prompts: IdeaPrompt[] }>("/api/nuggets"),
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
};

export { ApiError };
