import type { BuildView } from "./build";
import { isWorkshopClosingDetail, notifyWorkshopClosing, type WorkshopStatus } from "./workshopLifecycle";

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

export type WorkingApproach = "" | "co_build" | "learn" | "lead";
export type ExplanationDetail = "" | "adaptive" | "brief" | "detailed";
export type EngagementPath = "" | "rapid" | "guided";
/** The attendee's own starting point, only ever what they said; "" = not set. */
export type Background = "" | "domain" | "data" | "developer" | "exploring";

export interface AttendeeProfile {
  schema_version: 1;
  revision: number;
  help_preference: "" | "guided" | "concise" | "technical";
  source: string;
  legacy_persona: string;
  /** How the attendee wants to work with their coding agent (W2); "" = not chosen. */
  working_approach?: WorkingApproach;
  explanation_detail?: ExplanationDetail;
  /** Default start for a new idea; a project's recorded path wins. */
  engagement_path?: EngagementPath;
  background?: Background;
  choice_sources?: Record<string, string>;
}

export interface ProfileSave {
  expected_revision: number;
  help_preference?: AttendeeProfile["help_preference"];
  working_approach?: WorkingApproach;
  explanation_detail?: ExplanationDetail;
  engagement_path?: EngagementPath;
  background?: Background;
}

export interface WizardIdea {
  id: string;
  label: string;
  outcome: string;
  prompt: string;
  industries: string[];
  intents: string[];
  products: string[];
  shape: string;
  technical: boolean;
  demo_tables: string[];
  /** The server verified these tables exist. Absent on an unreadable catalog,
   * where the honest answer is that we do not know — never inferred from
   * `demo_tables` being non-empty, which is a claim we cannot check. */
  data_ready?: boolean;
  /** Whether this card uses seeded demo tables (`demo`) or generated data. */
  data_mode?: "demo" | "generate";
  selection_token?: string;
  selection_expires_at?: number;
  schema_version?: number;
  source?: "catalog" | "generated";
  model?: string;
  content_digest?: string;
  fit_reason?: string;
  first_version?: string;
  assumptions?: string[];
  unresolved?: string[];
}

export interface WizardBrief {
  schema_version?: number;
  revision?: number;
  stage?: "draft" | "complete" | "skipped";
  selected_idea?: WizardIdea | null;
  selection_token?: string;
  discovery_record_id?: string;
  words_source?: "attendee" | "legacy_unverified" | "confirmed_goal";
  record_id: string;
  what_building: string;
  industry: string;
  industry_stated: boolean;
  intent: string;
  idea_id: string;
  current_stack: string[];
  persona: string;
  seen: boolean;
  skipped: boolean;
  completed_at: string;
  /** How they chose to start this idea: rapid prototype, guided ideation, or unchosen. */
  engagement_path?: EngagementPath;
}

export interface WizardState {
  brief: WizardBrief;
  /** False when the workshop was created with the wizard switched off. */
  enabled: boolean;
  should_show: boolean;
  should_ask_industry?: boolean;
  default_industry: string;
  /** Every industry the seed notebook knows, offered whether or not this
   * deployment has them. Gating the picker on live inventory is what made an
   * unset catalog remove the question instead of the answer. */
  industries: string[];
  industry_labels?: Record<string, string>;
  /** The subset of `industries` with data actually seeded here. Badged, not
   * filtered on. */
  seeded_industries?: string[];
  demo_data_available: boolean;
  intents: string[];
  intent_labels?: Record<string, string>;
  stacks?: string[];
  ideas: WizardIdea[];
  capture_enabled: boolean;
  llm_wizard?: { enabled: boolean; model: string };
}

export interface WizardSave {
  expected_revision?: number;
  operation?: "draft" | "complete" | "skip" | "clear" | "change" | "industry";
  selection_token?: string;
  what_building?: string;
  industry?: string;
  industry_stated?: boolean;
  intent?: string;
  idea_id?: string;
  current_stack?: string[];
  persona?: string;
  skipped?: boolean;
  display_name?: string;
  engagement_path?: EngagementPath;
}

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
  workshop: WorkshopStatus;
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
  onboarding_wizard: { enabled: boolean };
  llm_wizard?: { enabled: boolean; model: string };
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
  /** When set, clicking the card opens the dedicated web app in a new tab. */
  external_url?: string;
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
  project_name?: string;
}

export interface WorkshopProject {
  name: string;
  goal: string;
  available: boolean;
  saved: boolean;
  missing: boolean;
}

/** Saved builds brought back after a container restart (server/build_restore.py). */
export interface BuildRestore {
  id: string;
  state: "checking" | "restoring" | "restored" | "partial" | "failed";
  restored: string[];
  failed: string[];
  active: string;
  saved_at: number | null;
}

export interface ProjectInventory {
  active: string;
  projects: WorkshopProject[];
  restore?: BuildRestore;
}

export interface ProjectSelection {
  name: string;
  goal?: string;
  new?: boolean;
  recover?: boolean;
}

export interface PriorSessionInfo extends SessionInfo {
  exited: true;
  exit_reason: string;
}

export interface SessionPayload {
  sessions: SessionInfo[];
  prior_sessions?: PriorSessionInfo[];
}

export interface SessionConflict {
  code: "session_conflict";
  message: string;
  active_session: Pick<SessionInfo, "id" | "agent_id" | "label">;
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

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    const message =
      typeof detail === "string"
        ? detail
        : typeof detail === "object" && detail !== null && "message" in detail
          ? String(detail.message)
          : `Request failed (${status})`;
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

export function sessionConflictFrom(error: unknown): SessionConflict | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const detail = error.detail;
  if (!detail || typeof detail !== "object") return null;
  const candidate = detail as Partial<SessionConflict>;
  const active = candidate.active_session;
  if (
    candidate.code !== "session_conflict" ||
    !active ||
    typeof active.id !== "string" ||
    typeof active.agent_id !== "string" ||
    typeof active.label !== "string"
  ) {
    return null;
  }
  return candidate as SessionConflict;
}

type WrongIdentityListener = (detail: string) => void;
const wrongIdentityListeners = new Set<WrongIdentityListener>();
let wrongIdentityLatched: string | null = null;

export function onWrongIdentity(listener: WrongIdentityListener): () => void {
  if (wrongIdentityLatched) listener(wrongIdentityLatched);
  wrongIdentityListeners.add(listener);
  return () => wrongIdentityListeners.delete(listener);
}

export function isWrongIdentityLocked(): boolean {
  return wrongIdentityLatched !== null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (wrongIdentityLatched && !path.startsWith("/api/admin/")) {
    throw new ApiError(403, wrongIdentityLatched);
  }
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    let detail: unknown = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    if (resp.status === 409 && isWorkshopClosingDetail(detail)) notifyWorkshopClosing();
    const errorCode = resp.headers?.get?.("X-Workshop-Error-Code") ?? "";
    const isWrongSeat =
      errorCode === "wrong_seat_identity" ||
      (typeof detail === "string" && detail.includes("This workshop instance is assigned to "));
    if (resp.status === 403 && isWrongSeat && !path.startsWith("/api/admin/")) {
      const msg = typeof detail === "string" ? detail : "This workshop seat is assigned to another attendee.";
      if (!wrongIdentityLatched) {
        wrongIdentityLatched = msg;
        wrongIdentityListeners.forEach((fn) => fn(msg));
      }
    }
    throw new ApiError(resp.status, detail);
  }
  return resp.json();
}

export const api = {
  build: () => request<BuildView>("/api/build"),
  buildRequest: (kind: "change" | "explain", text: string) =>
    request<{ detail: string }>("/api/build/request", { method: "POST", body: JSON.stringify({ kind, text }) }),
  buildDecide: (decision: { question_id?: string; answer?: string; confirm_direction?: boolean; path?: "rapid" | "guided" }) =>
    request<{ revision: number }>("/api/build/decide", { method: "POST", body: JSON.stringify(decision) }),
  buildStop: () => request<{ stopped: boolean; detail: string }>("/api/build/stop", { method: "POST" }),
  /** Export my code: the ZIP downloads, and a verified copy stays in the workshop Volume. */
  exportCode: async (): Promise<{ blob: Blob; filename: string; volumePath: string }> => {
    const resp = await fetch("/api/export", { method: "POST" });
    if (!resp.ok) {
      let detail: unknown = resp.statusText;
      try { detail = (await resp.json()).detail ?? detail; } catch { /* non-JSON error body */ }
      throw new ApiError(resp.status, detail);
    }
    const disposition = resp.headers.get("Content-Disposition") ?? "";
    const filename = /filename="?([^";]+)"?/.exec(disposition)?.[1] ?? "workshop-code.zip";
    return { blob: await resp.blob(), filename, volumePath: resp.headers.get("X-Workshop-Export-Path") ?? "" };
  },
  attachImage: (body: { filename: string; data_base64: string; project_name?: string }) =>
    request<{ path: string; relative_path: string; bytes: number; project: string }>(
      "/api/projects/attach-image",
      { method: "POST", body: JSON.stringify(body) }
    ),
  retryAgentSetup: (agentId: string) =>
    request<{ retrying: boolean }>(`/api/agents/${encodeURIComponent(agentId)}/retry-setup`, { method: "POST" }),
  config: () => request<AppConfig>("/api/config"),
  workshopStatus: () => request<WorkshopStatus>("/api/workshop-status"),
  setupStatus: () =>
    request<{
      steps: Record<string, { status: string; error: string | null }>;
      ready: Record<string, boolean>;
      installing: boolean;
    }>("/api/setup-status"),
  agents: () =>
    request<{ agents: AgentInfo[]; credential: CredentialStatus }>("/api/agents"),
  sessions: () => request<SessionPayload>("/api/sessions"),
  projects: () => request<ProjectInventory>("/api/projects"),
  // A read: closeout must never wait on, or fail because of, a saved-copy check.
  savedProjects: () => request<ProjectInventory>("/api/projects/saved"),
  selectProject: (body: ProjectSelection) =>
    request<ProjectInventory>("/api/projects/select", { method: "POST", body: JSON.stringify(body) }),
  recoverProject: (name: string) =>
    request<ProjectInventory>(`/api/projects/${encodeURIComponent(name)}/recover`, { method: "POST" }),
  createSession: (agentId: string, replacesAgentId?: string, projectName?: string, hasStarterPrompt = false) =>
    request<{ session: SessionInfo }>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({
        agent_id: agentId,
        ...(replacesAgentId ? { replaces_agent_id: replacesAgentId } : {}),
        ...(projectName !== undefined ? { project_name: projectName } : {}),
        ...(hasStarterPrompt ? { has_starter_prompt: true } : {}),
      }),
    }),
  closeSession: (id: string) => request(`/api/sessions/${id}`, { method: "DELETE" }),
  ackPriorSession: (id: string) =>
    request(`/api/sessions/prior/${encodeURIComponent(id)}`, { method: "DELETE" }),
  typeIntoSession: (id: string, text: string, deliveryId?: string) =>
    request(`/api/sessions/${id}/type`, {
      method: "POST",
      body: JSON.stringify({ text, ...(deliveryId ? { delivery_id: deliveryId } : {}) }),
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
  profile: () => request<AttendeeProfile>("/api/profile"),
  saveProfile: (body: ProfileSave) =>
    request<AttendeeProfile>("/api/profile", { method: "POST", body: JSON.stringify(body) }),
  wizard: (industry?: string, query?: string, signal?: AbortSignal) => {
    const params = new URLSearchParams();
    if (industry !== undefined) params.set("industry", industry);
    if (query) params.set("q", query);
    const qs = params.toString();
    return request<WizardState>(qs ? `/api/wizard?${qs}` : "/api/wizard", { signal });
  },
  saveWizard: (body: WizardSave) =>
    request<{ brief: WizardBrief; starter_prompt: string }>("/api/wizard", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  wizardSuggest: (
    body: { text: string; industry: string; industry_locked?: boolean; intent?: string; persona?: string },
    signal?: AbortSignal
  ) =>
    request<{ industry: string; ideas: WizardIdea[]; source: string; fallback_reason?: string }>(
      "/api/wizard/suggest",
      { method: "POST", body: JSON.stringify(body), signal }
    ),
  wizardSurprise: (industry?: string, signal?: AbortSignal) =>
    request<{ idea: WizardIdea | null }>(
      industry
        ? `/api/wizard/surprise?industry=${encodeURIComponent(industry)}`
        : "/api/wizard/surprise", { signal }
    ),
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
