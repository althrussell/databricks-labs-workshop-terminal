import { useCallback, useEffect, useRef, useState } from "react";
import {
  Award,
  BookOpen,
  Download,
  GraduationCap,
  House,
  ImagePlus,
  Link as LinkIcon,
  MessageSquare,
  Rocket,
  ShieldCheck,
  Sparkles,
  Bot,
  X,
  Settings2,
} from "lucide-react";
import {
  api,
  AgentInfo,
  ApiError,
  AppConfig,
  PriorSessionInfo,
  BuildRestore,
  ProjectInventory,
  ProjectSelection,
  SessionInfo,
  WizardBrief,
  onWrongIdentity,
  relaunchAndAcknowledge,
  sessionConflictFrom,
  splitSessionPayload,
} from "./api";
import databricksLogo from "./assets/databricks-logo.svg";
import BannerBar from "./components/BannerBar";
import Hero from "./components/Hero";
import Wizard from "./components/Wizard";
import { PreferencesDialog } from "./components/HelpPreference";
import LaunchBar from "./components/LaunchBar";
import NuggetsPane from "./components/NuggetsPane";
import OperatorPanel from "./components/OperatorPanel";
import SignInNotice from "./components/SignInNotice";
import { NetworkNotice, NetworkStatusControl } from "./components/NetworkNotice";
import {
  RECHECK_COOLDOWN_MS,
  nextRetryDelayMs,
  runNetworkCheck,
  type NetworkCheckResult,
  type NetworkStatusKind,
} from "./networkCheck";
import RaiseHandButton from "./components/RaiseHandButton";
import HelpChatPanel from "./components/HelpChatPanel";
import ToastHost from "./components/ToastHost";
import TerminalView from "./components/TerminalView";
import AgentSwitchDialog from "./components/AgentSwitchDialog";
import ProjectBar from "./components/ProjectBar";
import YourBuild from "./components/YourBuild";
import ResponsiveNav from "./components/ResponsiveNav";
import { goalChangePrompt, keepSavedCopies, recoverWithConfirmation, restoreInProgress, restoreNotice } from "./projects";
import { bindIdentityRefresh, onAppEvent } from "./events";
import { ideaPromptAction } from "./ideation";
import { friendlyError, type RecoveryAction } from "./errors";
import { codeFromMessage, postAttendeeError, reportAttendeeError } from "./telemetry";
import {
  ActiveSessionChanged,
  agentSelection,
  closeThenCreate,
  resolveSessionConflict,
  viewOnLoad,
} from "./sessionSwitch";
import { wizardDeliveryId } from "./wizardRequests";
import {
  advanceWorkshopState,
  isWorkshopClosingDetail,
  notifyWorkshopClosing,
  onWorkshopClosing,
  workshopNotice,
  type WorkshopState,
} from "./workshopLifecycle";

// An attendee who needs an idea gets one short choice before implementation.
// This is typed UNSENT, like the other idea chips.
/** Type into a just-launched session as soon as it will take input.
 *
 * A blind 4s wait used to sit between launch and the first prompt. Trying
 * immediately, then once more after a short pause, lands the text as soon as
 * the PTY is accepting it without making every attendee wait the worst case.
 */
async function typeWhenSessionReady(sessionId: string, text: string, deliveryId: string = crypto.randomUUID(), signal?: AbortSignal) {
  if (signal?.aborted) return;
  try {
    await api.typeIntoSession(sessionId, text, deliveryId);
  } catch (caught) {
    if (signal?.aborted) return;
    if (caught instanceof ApiError && isWorkshopClosingDetail(caught.detail)) throw caught;
    if (caught instanceof ApiError && caught.message.includes("delivery was interrupted")) throw caught;
    await new Promise((r) => setTimeout(r, 800));
    if (signal?.aborted) return;
    await api.typeIntoSession(sessionId, text, deliveryId);
  }
}

const LINK_ICONS: Record<string, typeof LinkIcon> = {
  "book-open": BookOpen,
  "graduation-cap": GraduationCap,
  rocket: Rocket,
  sparkles: Sparkles,
  link: LinkIcon,
};

/** The restore notice an attendee dismissed (server/build_restore.py), so it is told once. */
const restoreSeenKey = "workshop.buildRestore.seen";

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [workshopState, setWorkshopState] = useState<WorkshopState | null>(null);
  const inputDisabled = workshopState !== "open";
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [priorSessions, setPriorSessions] = useState<PriorSessionInfo[]>([]);
  const [launching, setLaunching] = useState<string | null>(null);
  const [pendingSwitch, setPendingSwitch] = useState<{
    agentId: string;
    starterPrompt?: string;
  } | null>(null);
  const [switching, setSwitching] = useState(false);
  const [switchError, setSwitchError] = useState("");
  const [certOpen, setCertOpen] = useState(false);
  const [certName, setCertName] = useState("");
  const [certBusy, setCertBusy] = useState(false);
  const [error, setError] = useState("");
  const [recovering, setRecovering] = useState(false);
  const [connectionLost, setConnectionLost] = useState(false);
  const [nuggetsCollapsed, setNuggetsCollapsed] = useState(() => window.matchMedia("(max-width: 700px)").matches);
  const [helpChatOpen, setHelpChatOpen] = useState(false);
  const [helpUnread, setHelpUnread] = useState(0);
  const [helpRaised, setHelpRaised] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [preferencesOpen, setPreferencesOpen] = useState(false);
  const [brief, setBrief] = useState<WizardBrief | null>(null);
  const [sessionsLoaded, setSessionsLoaded] = useState(false);
  const [projectInventory, setProjectInventory] = useState<ProjectInventory | null>(null);
  const projectInventoryRef = useRef<ProjectInventory | null>(null);
  const projectRequest = useRef(0);
  const projectMutation = useRef(false);
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const [projectLoadError, setProjectLoadError] = useState("");
  const [buildRestore, setBuildRestore] = useState<BuildRestore | null>(null);
  const [restoreSeen, setRestoreSeen] = useState(() => {
    try { return localStorage.getItem(restoreSeenKey) ?? ""; } catch { return ""; }
  });
  const [savedChecked, setSavedChecked] = useState(false);
  const [keepaliveNonce, setKeepaliveNonce] = useState(0);
  const [networkStatus, setNetworkStatus] = useState<NetworkStatusKind>("checking");
  const [networkResult, setNetworkResult] = useState<NetworkCheckResult | null>(null);
  const [wrongIdentityDetail, setWrongIdentityDetail] = useState<string | null>(null);
  const [exportBusy, setExportBusy] = useState(false);
  const [attachBusy, setAttachBusy] = useState(false);
  // F-046: the only sign an attach worked was a file path typed into the
  // terminal. Show what was attached, and what to do next, for a little while.
  const [attached, setAttached] = useState<{ name: string; url: string } | null>(null);
  const imageInputRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    if (!attached) return;
    const timer = window.setTimeout(() => setAttached(null), 20000);
    return () => {
      window.clearTimeout(timer);
      URL.revokeObjectURL(attached.url);
    };
  }, [attached]);
  const networkLastRunAt = useRef(0);
  const networkRunning = useRef(false);
  const networkRetryAttempt = useRef(0);
  const savedAutoCheck = useRef(false);
  const helpChatOpenRef = useRef(false);
  const wizardChecked = useRef(false);
  const [view, setView] = useState<"home" | "agent" | "operator">(
    location.pathname.startsWith("/operator") ? "operator" : "home"
  );

  const refreshAgents = useCallback(() => {
    api.agents().then((data) => setAgents(data.agents)).catch(() => undefined);
  }, []);

  const refreshSessions = useCallback(async (): Promise<SessionInfo | null> => {
    const data = await api.sessions();
    const { live, prior } = splitSessionPayload(data);
    const active = live[0] ?? null;
    setSession(active);
    setPriorSessions(prior);
    return active;
  }, []);

  const acceptProjects = useCallback((value: ProjectInventory) => {
    projectInventoryRef.current = value;
    setProjectInventory(value);
    setProjectsLoaded(true);
    setProjectLoadError("");
  }, []);

  const refreshProjects = useCallback(async (saved = false) => {
    if (projectMutation.current) return;
    const requestId = ++projectRequest.current;
    try {
      const value = saved ? await api.savedProjects() : await api.projects();
      if (!saved) setBuildRestore(value.restore ?? null);
      if (requestId === projectRequest.current) acceptProjects(saved ? value : keepSavedCopies(value, projectInventoryRef.current));
    } catch (caught) {
      if (requestId === projectRequest.current) setProjectLoadError(caught instanceof Error ? caught.message : String(caught));
      throw caught;
    } finally { setProjectsLoaded(true); }
  }, [acceptProjects]);

  const handleGatewayLimit = useCallback(
    (code: "gateway_allowance_exhausted" | "gateway_rate_limited") => {
      setError(code);
      reportAttendeeError(code, code, {
        agentId: session?.agent_id,
        sessionId: session?.id,
      });
    },
    [session?.agent_id, session?.id]
  );

  /* Stable across renders, because the wizard treats a changed prop identity as
   * a reason to re-run its mount effect — and Home re-renders every few seconds
   * while it polls agent install progress. An inline arrow here used to wipe
   * whatever the attendee was typing into the wizard. */
  // The operator's create-time choice, and only once it is known. Reading an
  // unloaded config as "on" was wrong for the one path that does not consult the
  // server again: "Change what I'm building" opens the wizard directly, so an
  // attendee with a saved brief and a slow or failed /api/config could reopen a
  // modal their run had switched off. Unknown therefore means unavailable, which
  // costs the control a moment of lateness and nothing else.
  const wizardAvailable = config?.onboarding_wizard.enabled === true && !inputDisabled;
  const wizardDismissalKey = config
    ? `wt-wizard-dismissed:v1:${config.workspace_url}:${config.user.email}`
    : "";

  const openWizard = useCallback(() => {
    if (!wizardAvailable) return;
    setWizardOpen(true);
  }, [wizardAvailable]);

  const closeWizard = useCallback((skipped = false) => {
    if (skipped && wizardDismissalKey) {
      try {
        sessionStorage.setItem(wizardDismissalKey, "true");
      } catch { /* Browser storage is optional; the mounted view still closes. */ }
    }
    setWizardOpen(false);
    // Pick up whatever the wizard saved so Home's recap line is right
    // immediately, rather than after the next reload.
    api.wizard().then((s) => setBrief(s.brief)).catch(() => undefined);
  }, [wizardDismissalKey]);

  const refreshIdentity = useCallback(async () => {
    setKeepaliveNonce((n) => n + 1);
    const cfg = await api.config();
    setConfig(cfg);
    setWorkshopState((current) => advanceWorkshopState(current, cfg.workshop.state));
    if (cfg.branding.brand_primary_color) {
      document.documentElement.style.setProperty(
        "--brand-primary",
        cfg.branding.brand_primary_color
      );
    }
    document.title = cfg.branding.event_name || `${cfg.branding.brand_name} Workshop`;
  }, []);

  const triggerNetworkCheck = useCallback((force = false) => {
    if (networkRunning.current) return;
    const now = Date.now();
    if (!force && networkLastRunAt.current > 0 && now - networkLastRunAt.current < RECHECK_COOLDOWN_MS) {
      return;
    }
    networkRunning.current = true;
    networkLastRunAt.current = now;
    setNetworkStatus("checking");
    void runNetworkCheck()
      .then((res) => {
        setNetworkResult(res);
        setNetworkStatus(res.verdict);
      })
      .finally(() => {
        networkRunning.current = false;
      });
  }, []);

  const networkNotOk = networkResult !== null && networkResult.verdict !== "ok";

  useEffect(() => {
    if (!config || inputDisabled) return;
    triggerNetworkCheck(false);
  }, [Boolean(config), inputDisabled, triggerNetworkCheck]);

  // A bad verdict must not outlive the problem (E2E 28 Oct: both seats stayed
  // "blocked" after a DNS outage cleared, because the check ran only on load
  // and the 10-minute cooldown swallowed the "online" event). While it is not
  // ok, coming back online or returning to the tab checks again straight away.
  useEffect(() => {
    if (!config || inputDisabled) return;
    const recheck = () => triggerNetworkCheck(networkNotOk);
    const onVisible = () => {
      if (document.visibilityState === "visible") recheck();
    };
    window.addEventListener("online", recheck);
    document.addEventListener("visibilitychange", onVisible);
    const conn = (navigator as unknown as { connection?: EventTarget }).connection;
    conn?.addEventListener?.("change", recheck);
    return () => {
      window.removeEventListener("online", recheck);
      document.removeEventListener("visibilitychange", onVisible);
      conn?.removeEventListener?.("change", recheck);
    };
  }, [Boolean(config), inputDisabled, networkNotOk, triggerNetworkCheck]);

  // ...and on its own, backing off from 30 s to every 5 minutes.
  useEffect(() => {
    if (!config || inputDisabled || !networkResult) return;
    if (networkResult.verdict === "ok") {
      networkRetryAttempt.current = 0;
      return;
    }
    const delay = nextRetryDelayMs(networkRetryAttempt.current);
    networkRetryAttempt.current += 1;
    const timer = window.setTimeout(() => triggerNetworkCheck(true), delay);
    return () => window.clearTimeout(timer);
  }, [Boolean(config), inputDisabled, networkResult, triggerNetworkCheck]);

  useEffect(() => onWorkshopClosing(() => {
    setWorkshopState((current) => advanceWorkshopState(current, "closing"));
    setConnectionLost(false);
    setError("");
  }), []);

  useEffect(() => {
    if (workshopState === "closed") return;
    let stopped = false;
    let running = false;
    const refresh = async () => {
      if (running) return;
      running = true;
      try {
        const status = await api.workshopStatus();
        if (!stopped) setWorkshopState((current) => advanceWorkshopState(current, status.state));
      } catch { /* Keep the last observed barrier on a transient failed read. */ }
      finally { running = false; }
    };
    void refresh();
    const interval = setInterval(() => { void refresh(); }, 10000);
    return () => { stopped = true; clearInterval(interval); };
  }, [workshopState === "closed"]);

  useEffect(() => {
    if (!inputDisabled) return;
    setWizardOpen(false);
    setPreferencesOpen(false);
    setCertOpen(false);
    setPendingSwitch(null);
  }, [inputDisabled]);

  useEffect(() => onWrongIdentity((detail) => {
    setWrongIdentityDetail(detail);
  }), []);

  useEffect(() => {
    if (wrongIdentityDetail) return;
    const refresh = () =>
      refreshIdentity().catch((e) =>
        setError(e instanceof Error ? e.message : String(e))
      );
    refresh();
    const stopIdentityRefresh = bindIdentityRefresh(refresh);

    refreshSessions()
      .then((active) => setView((current) => viewOnLoad(current, active)))
      .catch((e) => setError(
        e instanceof Error ? e.message : String(e)
      ))
      .finally(() => setSessionsLoaded(true));

    refreshAgents();
    void refreshProjects().catch(() => undefined);
    const interval = setInterval(refreshAgents, 5000);
    const projectInterval = setInterval(() => { void refreshProjects().catch(() => undefined); }, 15000);
    const keepaliveInterval = setInterval(() => {
      refresh();
    }, 600000);
    return () => {
      clearInterval(interval);
      clearInterval(projectInterval);
      clearInterval(keepaliveInterval);
      stopIdentityRefresh();
    };
  }, [wrongIdentityDetail, refreshAgents, refreshIdentity, refreshSessions, refreshProjects]);

  // A restarted container restores saved builds in the background; follow it
  // closely until it finishes rather than at the 15 s cadence.
  useEffect(() => {
    if (!restoreInProgress(buildRestore)) return;
    const timer = setTimeout(() => { void refreshProjects().catch(() => undefined); }, 3000);
    return () => clearTimeout(timer);
  }, [buildRestore, refreshProjects]);

  // An empty list on first load may just mean saved copies have not been looked
  // at yet. Look once, quietly, instead of telling the attendee "No builds yet".
  useEffect(() => {
    if (savedAutoCheck.current || !projectInventory || restoreInProgress(buildRestore) || workshopState !== "open") return;
    savedAutoCheck.current = true;
    if (projectInventory.active || projectInventory.projects.length) { setSavedChecked(true); return; }
    api.savedProjects()
      .then((value) => {
        // Unprompted, so only a well-formed list may replace what is shown.
        if (Array.isArray(value?.projects) && !projectMutation.current && !projectInventoryRef.current?.projects.length) {
          acceptProjects(value);
        }
      })
      .catch(() => undefined)
      .finally(() => setSavedChecked(true));
  }, [projectInventory, buildRestore, workshopState, acceptProjects]);

  const dismissRestore = useCallback(() => {
    const id = buildRestore?.id ?? "";
    setRestoreSeen(id);
    try { localStorage.setItem(restoreSeenKey, id); } catch { /* the notice simply returns on reload */ }
  }, [buildRestore]);
  const restoreMessage = buildRestore && buildRestore.id !== restoreSeen ? restoreNotice(buildRestore) : "";

  useEffect(() => {
    helpChatOpenRef.current = helpChatOpen;
  }, [helpChatOpen]);

  useEffect(() => {
    const narrow = window.matchMedia("(max-width: 700px)");
    const collapse = (event: MediaQueryListEvent) => { if (event.matches) setNuggetsCollapsed(true); };
    narrow.addEventListener("change", collapse);
    return () => narrow.removeEventListener("change", collapse);
  }, []);

  /* The wizard opens once, on the first arrival, and never again.
   *
   * `should_show` normally comes from the durable brief, so reloads and other
   * tabs respect Skip. The current tab also remembers immediate dismissal in
   * sessionStorage, covering a slow or failed skip write without trapping the
   * attendee. An explicit goal-edit action can still reopen the wizard.
   *
   * Suppressed when a session already exists — a returning attendee is mid-build,
   * and a modal asking what they intend to build is at best late. That check has
   * to wait for the session list to arrive: asking on mount only ever reads the
   * empty array the state starts as, which is indistinguishable from a first
   * arrival and opens the modal over somebody's running terminal. */
  useEffect(() => {
    if (!config || inputDisabled || !sessionsLoaded || !projectsLoaded || wizardChecked.current) return;
    // Once per load. Without the guard the wizard would reopen the moment an
    // attendee closed their last terminal, which is precisely when they are
    // least in the mood for it.
    wizardChecked.current = true;
    let dismissed = false;
    try {
      dismissed = sessionStorage.getItem(wizardDismissalKey) === "true";
    } catch { /* The server's durable state remains the normal dismissal source. */ }
    api
      .wizard()
      .then((state) => {
        setBrief(state.brief);
        if (state.enabled && state.should_show && !session && !projectInventoryRef.current?.active && !dismissed) {
          setWizardOpen(true);
        }
      })
      .catch(() => { if (config.onboarding_wizard.enabled && !session && !projectInventoryRef.current?.active && !dismissed) setWizardOpen(true); });
  }, [config, inputDisabled, sessionsLoaded, projectsLoaded, session, wizardDismissalKey]);

  useEffect(() => {
    if (config?.help) setHelpRaised(config.help.raised);
  }, [config?.help]);

  useEffect(
    () =>
      onAppEvent((event) => {
        if (event.t === "workshop_closing") notifyWorkshopClosing();
        if (event.t === "connection_lost") {
          setConnectionLost(true);
          reportAttendeeError("websocket_lost", event.reason);
        }
        if (event.t === "reconnected") setConnectionLost(false);
        if (event.t === "help_state") setHelpRaised(event.raised);
        if (event.t === "help_message" && event.sender_role === "operator") {
          // The toast does the noticing; this keeps the durable inbox honest so
          // a dismissed or missed toast is still recoverable from the panel.
          if (!helpChatOpenRef.current) setHelpUnread((n) => n + 1);
        }
      }),
    []
  );

  // Stop polling agents once everything is installed.
  const allReady = agents.length > 0 && agents.every((a) => a.ready);
  useEffect(() => {
    if (allReady) refreshAgents();
  }, [allReady, refreshAgents]);

  async function launch(
    agentId: string,
    repairRetried = false,
    starterPrompt = "",
    conflictRetried = false,
    signal?: AbortSignal
  ): Promise<SessionInfo | null> {
    if (signal?.aborted || inputDisabled) return null;
    setLaunching(agentId);
    setError("");
    try {
      const { session: created } = await api.createSession(agentId, undefined, projectInventoryRef.current?.active,
        Boolean(starterPrompt));
      setSession(created);
      setView("agent");
      return created;
    } catch (e) {
      if (signal?.aborted) return null;
      if (e instanceof ApiError && isWorkshopClosingDetail(e.detail)) return null;
      const conflict = sessionConflictFrom(e);
      if (conflict) {
        try {
          const active = await refreshSessions();
          if (signal?.aborted) return null;
          const resolution = resolveSessionConflict(active, agentId, starterPrompt);
          if (resolution.action === "focus") {
            setView("agent");
            return resolution.active;
          }
          if (resolution.action === "confirm") {
            setSwitchError("");
            setPendingSwitch({
              agentId: resolution.requestedAgentId,
              starterPrompt: resolution.starterPrompt,
            });
          }
          if (resolution.action === "missing") {
            // The conflicting session ended between POST and refresh. Retry
            // once: the slot is now free and the attendee's prompt still
            // belongs to this launch. A bound avoids spinning if another tab
            // keeps winning and closing the slot.
            if (!conflictRetried) {
              return await launch(agentId, repairRetried, starterPrompt, true, signal);
            }
            setError(
              "The active agent changed while this one was opening. Try again."
            );
          }
        } catch (refreshError) {
          if (signal?.aborted) return null;
          setError(
            refreshError instanceof Error ? refreshError.message : String(refreshError)
          );
        }
        return null;
      }
      const message = e instanceof Error ? e.message : String(e);
      // Same string on both sides of the glass: what the attendee reads here
      // is what an operator finds in diagnostics, without waiting to be told.
      // The reply says whether the server repaired the credential while
      // answering, in which case one silent retry beats an error the attendee
      // would have cleared by clicking the same button again.
      const canRetry = await postAttendeeError(
        codeFromMessage(message, "session_start_failed"),
        message,
        { agentId }
      );
      if (signal?.aborted) return null;
      if (canRetry && !repairRetried) {
        return await launch(agentId, true, starterPrompt, conflictRetried, signal);
      }
      setError(message);
      return null;
    } finally {
      setLaunching(null);
    }
  }

  async function runRecovery(action: RecoveryAction) {
    if (action === "reload") {
      location.reload();
      return;
    }
    if (inputDisabled) return;
    setRecovering(true);
    try {
      const { recovered } = await api.recover();
      if (recovered) {
        setError("");
        refreshAgents();
      }
    } catch {
      /* the banner is already up; a failed recovery adds nothing to read */
    } finally {
      setRecovering(false);
    }
  }

  async function relaunchPrior(prior: PriorSessionInfo) {
    try {
      if (prior.project_name && prior.project_name !== projectInventoryRef.current?.active) {
        acceptProjects(await api.selectProject({ name: prior.project_name }));
      }
      await relaunchAndAcknowledge(
        prior,
        launch,
        api.ackPriorSession
      );
      // A successful acknowledgement consumes the ghost even if replacement
      // launch fails; the attendee can use the normal launch action afterward.
      setPriorSessions((current) =>
        current.filter((item) => item.id !== prior.id)
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function selectBuild(selection: ProjectSelection, stopSessionId?: string): Promise<string> {
    projectMutation.current = true;
    ++projectRequest.current;
    try {
      if (stopSessionId) {
        try { await api.closeSession(stopSessionId); }
        catch (caught) { if (!(caught instanceof ApiError) || caught.status !== 404) throw caught; }
        const active = await refreshSessions();
        if (active) throw new Error("Another agent became active. Confirm that agent before switching builds.");
      }
      const value = selection.recover
        ? await recoverWithConfirmation(selection.name, projectInventoryRef.current?.active, api.recoverProject, api.projects)
        : await api.selectProject(selection);
      acceptProjects(value);
      if (!selection.new && !selection.recover && selection.goal !== undefined) {
        const active = await refreshSessions();
        if (active) {
          try {
            await typeWhenSessionReady(active.id, goalChangePrompt(selection.name, selection.goal));
            setView("agent");
            return "Goal saved and typed into your agent. Press Enter in the terminal to request the change.";
          } catch (caught) {
            throw new Error(`Goal saved, but the change could not be typed. Tell your agent the new goal or retry. ${caught instanceof Error ? caught.message : String(caught)}`);
          }
        }
        return "Goal saved. Your next agent reads this build's context; tell it when you're ready to make the change.";
      }
      setSession(null); setView("home");
      return selection.recover
        ? "Source recovered and selected. Choose an agent to continue; its previous conversation and Git history were not restored."
        : `${selection.name} is selected. Choose an agent to ${selection.new ? "begin" : "continue"}.`;
    } finally { projectMutation.current = false; }
  }

  async function requestAgent(agentId: string, starterPrompt = "") {
    const target = agents.find((a) => a.id === agentId);
    const externalUrl = target?.external_url || (agentId === "omnigent" && config?.omnigent_remote?.enabled ? config.omnigent_remote.url : "");
    if (externalUrl) {
      window.open(externalUrl, "_blank", "noopener,noreferrer");
      return;
    }
    const selection = agentSelection(session, agentId);
    if (selection === "focus") {
      setView("agent");
      if (starterPrompt && session) {
        await api.typeIntoSession(session.id, starterPrompt);
      }
      return;
    }
    if (selection === "confirm") {
      setSwitchError("");
      setPendingSwitch({ agentId, starterPrompt });
      return;
    }
    const created = await launch(agentId, false, starterPrompt);
    if (created && starterPrompt) {
      await typeWhenSessionReady(created.id, starterPrompt);
    }
  }

  /** Launch from the wizard's last step, carrying the first prompt in.
   *
   * Deliberately the same typing mechanism the ideation chips use: the prompt
   * lands at the agent's prompt UNSENT so the attendee presses Enter. The wizard
   * hands them a loaded first move; it does not take the move for them, and an
   * agent that started talking on its own would undercut the whole point of
   * putting them in front of a terminal.
   */
  async function launchFromWizard(agentId: string, starterPrompt: string, signal: AbortSignal) {
    if (signal.aborted) return;
    const target = agents.find((a) => a.id === agentId);
    const externalUrl = target?.external_url || (agentId === "omnigent" && config?.omnigent_remote?.enabled ? config.omnigent_remote.url : "");
    if (externalUrl) {
      window.open(externalUrl, "_blank", "noopener,noreferrer");
      setWizardOpen(false);
      return;
    }
    if (session && !session.exited) {
      if (session.agent_id !== agentId) {
        throw new Error("An agent is already open. Choose that agent, or close it before opening another.");
      }
      const deliveryId = await wizardDeliveryId(session.id, starterPrompt);
      if (signal.aborted) return;
      await api.typeIntoSession(session.id, starterPrompt, deliveryId);
      if (signal.aborted) return;
      setView("agent");
    } else {
      const created = await launch(agentId, false, starterPrompt, false, signal);
      // Keep ownership of an already requested session, but stop the wizard's
      // queued prompt delivery when the attendee dismisses onboarding.
      if (signal.aborted) return;
      if (!created) throw new Error("The agent could not open. Try again, or choose another ready agent.");
      const createdDeliveryId = await wizardDeliveryId(created.id, starterPrompt);
      await typeWhenSessionReady(created.id, starterPrompt, createdDeliveryId, signal);
    }
    if (signal.aborted) return;
    setWizardOpen(false);
  }

  // Ideation chips / insight-card prompts: type the text into the attendee's
  // agent session UNSENT — they press Enter. Opens a coding agent if none, and
  // one this workshop actually offers: naming Claude here launched an agent an
  // operator may have withheld, which failed and left the attendee with an error
  // where the sentence they clicked should have been.
  async function ideaToSession(prompt: string) {
    const action = ideaPromptAction(agents, session ? [session] : [], prompt);
    if (action.action === "unavailable") {
      // Nothing open and nothing to open it with. Only reachable on a
      // workshop whose agent selection matched nothing in the catalogue, but
      // a chip that quietly does nothing when clicked is worse than saying so.
      if (agents.length > 0) {
        setError(
          "This workshop offers no coding agent to type that into — choose an agent first."
        );
      }
      return;
    }
    if (action.action === "request") {
      try {
        await requestAgent(action.agentId, action.starterPrompt);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
      return;
    }
    setView("agent");
    try {
      await api.typeIntoSession(action.sessionId, action.prompt);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function closeSession(id: string) {
    try {
      await api.closeSession(id);
      removeSession(id);
    } catch (closeFailure) {
      if (closeFailure instanceof ApiError && closeFailure.status === 404) {
        removeSession(id);
        return;
      }
      try {
        await refreshSessions();
      } catch {
        /* Keep the visible session; a reload remains an honest recovery. */
      }
      setError(
        closeFailure instanceof Error ? closeFailure.message : String(closeFailure)
      );
    }
  }

  const removeSession = useCallback((id: string) => {
    setSession((current) => {
      if (current?.id !== id) return current;
      setView((currentView) => (currentView === "agent" ? "home" : currentView));
        return null;
    });
  }, []);

  const finishSession = useCallback((id: string) => {
    // Keep the mounted terminal and its final output until the attendee closes
    // or relaunches it. Removing it here used to hide startup errors.
    setSession((current) =>
      current?.id === id ? { ...current, exited: true } : current
    );
  }, []);

  async function confirmSwitch() {
    if (!session || !pendingSwitch) return;
    const current = session;
    const requested = pendingSwitch;
    setSwitching(true);
    setSwitchError("");
    setError("");
    try {
      const created = await closeThenCreate(current, requested.agentId, {
        close: api.closeSession,
        refresh: refreshSessions,
        create: async (agentId, replacesAgentId) =>
          (await api.createSession(agentId, replacesAgentId, projectInventoryRef.current?.active,
            Boolean(requested.starterPrompt))).session,
      });
      setSession(created);
      setView("agent");
      setPendingSwitch(null);
      if (requested.starterPrompt) {
        await typeWhenSessionReady(created.id, requested.starterPrompt);
      }
    } catch (switchFailure) {
      let active: SessionInfo | null = null;
      try {
        active = await refreshSessions();
      } catch {
        /* Keep the current screen until the attendee can retry or reload. */
      }
      if (switchFailure instanceof ActiveSessionChanged) {
        active = switchFailure.active;
        setSession(active);
      }
      if (active?.agent_id === requested.agentId) {
        setPendingSwitch(null);
        setView("agent");
        if (requested.starterPrompt) {
          try {
            await typeWhenSessionReady(active.id, requested.starterPrompt);
          } catch (promptFailure) {
            setError(
              promptFailure instanceof Error
                ? promptFailure.message
                : String(promptFailure)
            );
          }
        }
      } else if (active) {
        setSwitchError(
          `${active.label} is still running. Close it before opening ${agentLabel(requested.agentId)}.`
        );
      } else {
        setPendingSwitch(null);
        setView("home");
        setError(
          switchFailure instanceof Error ? switchFailure.message : String(switchFailure)
        );
      }
    } finally {
      setSwitching(false);
    }
  }

  function openCertificate() {
    if (inputDisabled) return;
    if (!certName && config) {
      // Lab identities are generic — prefill a readable guess, let them fix it.
      const local = config.user.email.split("@")[0].replace(/[._+-]+/g, " ").trim();
      setCertName(local.replace(/\b\w/g, (ch) => ch.toUpperCase()));
    }
    setCertOpen(true);
  }

  async function downloadCertificate() {
    if (inputDisabled) return;
    const name = certName.trim();
    if (!name) return;
    setCertBusy(true);
    try {
      const resp = await fetch(`/api/certificate?name=${encodeURIComponent(name)}`);
      if (resp.status === 409 && isWorkshopClosingDetail((await resp.clone().json()).detail)) {
        notifyWorkshopClosing();
        return;
      }
      if (!resp.ok) throw new Error("Certificate generation failed — try again");
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "databricks-workshop-certificate.pdf";
      a.click();
      URL.revokeObjectURL(url);
      setCertOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCertBusy(false);
    }
  }

  async function downloadAllCode() {
    if (inputDisabled || exportBusy) return;
    setExportBusy(true);
    try {
      const { blob, filename } = await api.exportCode();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = filename;
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setExportBusy(false);
    }
  }

  async function attachImageFile(file: File) {
    if (inputDisabled || attachBusy) return;
    setAttachBusy(true);
    try {
      const dataUrl = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ""));
        reader.onerror = () => reject(new Error("Could not read image file"));
        reader.readAsDataURL(file);
      });
      const res = await api.attachImage({
        filename: file.name || "screenshot.png",
        data_base64: dataUrl,
        project_name: projectInventory?.active || undefined,
      });
      if (session && !session.exited) {
        await api.typeIntoSession(session.id, `${res.path} `);
      }
      setAttached({ name: file.name || "image", url: URL.createObjectURL(file) });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setAttachBusy(false);
    }
  }

  if (wrongIdentityDetail) {
    return (
      <div className="boot-screen" role="alert">
        <div style={{ maxWidth: "480px", textAlign: "center", lineHeight: 1.5 }}>
          <h2 style={{ marginBottom: "8px" }}>Signed in as a different user</h2>
          <p>{wrongIdentityDetail}</p>
        </div>
      </div>
    );
  }

  if (!config && !error) {
    return <div className="boot-screen">Loading workshop…</div>;
  }

  const branding = config?.branding;
  const shellLinks = config?.shell.links ?? [];
  const showNuggets = config?.shell.features.nuggets_pane !== false;
  const isAdmin = config?.user.is_admin ?? false;
  const agentLabel = (agentId: string) =>
    agents.find((agent) => agent.id === agentId)?.label ?? agentId;

  return (
    <div className="app">
      <header className="header">
        <div className="header-brand">
          <img
            src={branding?.brand_logo_url || databricksLogo}
            alt={branding?.brand_name ?? "Databricks"}
            className="brand-logo"
          />
          {branding?.cobranded && (
            <img src={databricksLogo} alt="Databricks" className="brand-logo brand-logo-secondary" />
          )}
          <div className="brand-text">
            <span className="brand-name">{branding?.brand_name ?? "Databricks"}</span>
            {branding?.event_name && <span className="event-name">{branding.event_name}</span>}
          </div>
        </div>
        <ResponsiveNav>
          {/* 1. Navigation (where am I) */}
          <button
            className={`operator-toggle ${view === "home" ? "operator-toggle-active" : ""}`}
            onClick={() => setView("home")}
            aria-label="Home"
            title={session && !session.exited ? `Back to Home — ${session.label} keeps running` : "Back to Home"}
          >
            <House size={16} aria-hidden="true" />
            <span className="nav-label">Home</span>
          </button>
          {session && (
            <button
              className={`operator-toggle ${view === "agent" ? "operator-toggle-active" : ""}`}
              onClick={() => setView("agent")}
              aria-label={session.label}
              title={`Back to ${session.label}`}
            >
              <Bot size={16} aria-hidden="true" />
              <span className="nav-label">{session.label}</span>
            </button>
          )}
          {isAdmin && (
            <button
              className={`operator-toggle ${view === "operator" ? "operator-toggle-active" : ""}`}
              onClick={() => setView(view === "operator" ? (session ? "agent" : "home") : "operator")}
              aria-label="Operator"
              title="Operator controls"
            >
              <ShieldCheck size={16} aria-hidden="true" />
              <span className="nav-label">Operator</span>
            </button>
          )}
          <span className="nav-divider" aria-hidden="true" />
          {/* 2. Resources (reference links) */}
          {shellLinks
            .filter((link) => !link.highlight)
            .map((link) => {
              const Icon = LINK_ICONS[link.icon] ?? LinkIcon;
              return (
                <a key={link.url} href={link.url} target="_blank" rel="noreferrer" aria-label={link.label} title={link.label}>
                  <Icon size={16} aria-hidden="true" />
                  <span className="nav-label">{link.label}</span>
                </a>
              );
            })}
          {config?.workspace_url && (
            <a
              href={config.workspace_url}
              target="_blank"
              rel="noopener noreferrer"
              aria-label="Workspace"
              title="Open the Databricks workspace"
            >
              <LinkIcon size={16} aria-hidden="true" />
              <span className="nav-label">Workspace</span>
            </a>
          )}
          {config?.omnigent_remote?.enabled && config.omnigent_remote.url && (
            <a
              href={config.omnigent_remote.url}
              target="_blank"
              rel="noopener noreferrer"
              aria-label="Omnigent"
              title="Open the dedicated Omnigent app"
            >
              <Rocket size={16} aria-hidden="true" />
              <span className="nav-label">Omnigent</span>
            </a>
          )}
          {/* 3. Actions, then the promoted CTA at the far right */}
          <button className="operator-toggle" disabled={inputDisabled} onClick={() => setPreferencesOpen(true)} aria-label="Agent preferences" title="Agent preferences — change how your agent helps">
            <Settings2 size={16} aria-hidden="true" /><span className="nav-label">Agent preferences</span>
          </button>
          <NetworkStatusControl
            status={networkStatus}
            result={networkResult}
            onRunAgain={() => triggerNetworkCheck(true)}
          />
          {Boolean(projectInventory?.active || (projectInventory?.projects && projectInventory.projects.length > 0)) && (
            <button
              className="operator-toggle"
              disabled={inputDisabled || exportBusy}
              onClick={() => void downloadAllCode()}
              aria-label="Download my code"
              title="Download a ZIP of your workshop projects"
            >
              <Download size={16} aria-hidden="true" />
              <span className="nav-label">{exportBusy ? "Exporting…" : "Download my code"}</span>
            </button>
          )}
          <button className="operator-toggle" disabled={inputDisabled} onClick={openCertificate} aria-label="Certificate" title="Download your certificate">
            <Award size={16} aria-hidden="true" />
            <span className="nav-label">Certificate</span>
          </button>
          <RaiseHandButton
            disabled={inputDisabled}
            initial={config?.help ?? null}
            raised={helpRaised}
            onRaisedChange={setHelpRaised}
          />
          <button
            type="button"
            className="help-chat-toggle"
            onClick={() => setHelpChatOpen(true)}
            aria-label="Help"
            title="Help — ask operators"
          >
            <MessageSquare size={16} aria-hidden="true" />
            <span className="nav-label">Help</span>
            {helpUnread > 0 ? <span className="help-chat-badge">{helpUnread}</span> : null}
          </button>
          {shellLinks
            .filter((link) => link.highlight)
            .map((link) => {
              const Icon = LINK_ICONS[link.icon] ?? LinkIcon;
              return (
                <a key={link.url} href={link.url} target="_blank" rel="noreferrer" className="link-highlight" aria-label={link.label} title={link.label}>
                  <Icon size={16} aria-hidden="true" />
                  <span className="nav-label">{link.label}</span>
                </a>
              );
            })}
        </ResponsiveNav>
        <div className="header-user" title={config?.user.email}>{config?.user.email}</div>
      </header>

      <BannerBar initial={config?.broadcast ?? null} />
      {workshopState && workshopState !== "open" && (
        <div className="banner banner-info" role="status" aria-live="polite">
          <span>{workshopNotice(workshopState)}</span>
        </div>
      )}
      {preferencesOpen && !inputDisabled && <PreferencesDialog onClose={() => setPreferencesOpen(false)} />}
      {/* The tab rule. Above the other banners because a stale sign-in is the
          cause of most of what they warn about. */}
      {!inputDisabled && <SignInNotice
        omnigentEnabled={!!config?.omnigent_remote.enabled}
        obo={config?.obo}
        onReload={() => location.reload()}
      />}
      {!inputDisabled && <NetworkNotice result={networkResult} />}
      {!inputDisabled && (config?.omnigent_remote.enabled || config?.obo.enabled) && keepaliveNonce > 0 && (
        <iframe
          src={`/api/auth/keepalive?t=${keepaliveNonce}`}
          title="Sign-in keepalive"
          aria-hidden="true"
          tabIndex={-1}
          style={{ display: "none" }}
        />
      )}
      <ToastHost
        onOpenHelp={() => setHelpChatOpen(true)}
        helpOpen={helpChatOpen}
      />
      <HelpChatPanel
        readOnly={inputDisabled}
        open={helpChatOpen}
        onClose={() => setHelpChatOpen(false)}
        unread={helpUnread}
        onClearUnread={() => setHelpUnread(0)}
        raised={helpRaised}
        onRaisedChange={setHelpRaised}
      />
      {connectionLost && !inputDisabled && (
        <div className="banner banner-warning">
          <span>
            Connection to workshop updates was lost. Reload the page to reconnect.
          </span>
        </div>
      )}
      {config && !config.credential.configured && (
        <div className="banner banner-warning">
          <span>
            Workshop credential not configured — the Databricks Apps runtime has not
            provided a valid app-identity OAuth bearer. Agents can't authenticate yet.
          </span>
        </div>
      )}
      {config && config.credential.configured && config.credential.state === "degraded" && (
        <div className="banner banner-warning">
          <span>
            Workshop credential is <strong>degraded</strong> — serving the explicit emergency
            WORKSHOP_PAT fallback without automatic refresh. Restore app-identity OAuth
            before the event.
          </span>
        </div>
      )}
      {config && config.credential.state === "unhealthy" && (
        <div className="banner banner-warning">
          <span>
            Workshop credential is <strong>unhealthy</strong>
            {config.credential.last_error ? ` — ${config.credential.last_error}` : ""}.
            Coding agents may fail to authenticate until this is fixed.
          </span>
        </div>
      )}
      {error && (workshopState === null || !inputDisabled) && (
        <div className="banner banner-warning">
          {/* A harness code is not a message and runner logs are not reachable
              from here. Show what happened and the one button that fixes it. */}
          <span>{friendlyError(error).message}</span>
          {friendlyError(error).action !== "none" && (
            <button
              className="banner-action"
              disabled={recovering}
              onClick={() => runRecovery(friendlyError(error).action)}
            >
              {recovering ? "Recovering…" : friendlyError(error).actionLabel}
            </button>
          )}
          <button className="icon-btn" onClick={() => setError("")}>
            <X size={14} />
          </button>
        </div>
      )}

      {view === "operator" ? (
        <OperatorPanel />
      ) : (
        <div className="main">
          <fieldset className="work-area workshop-inputs" disabled={inputDisabled} aria-label="Workshop activities">
            <ProjectBar inventory={projectInventory} session={session} loading={!projectsLoaded} loadError={projectLoadError}
              onRefresh={refreshProjects} onSelect={selectBuild} savedChecked={savedChecked}
              restoreNotice={restoreMessage}
              onDismissRestore={restoreInProgress(buildRestore) ? undefined : dismissRestore} />
            <YourBuild refreshKey={[projectInventory?.active, session?.id, session?.exited]}
              onGetHelp={() => setHelpChatOpen(true)} />
            {session && view === "agent" && (
              <div className="toolbar">
                <LaunchBar agents={agents} launching={launching} onLaunch={requestAgent} />
                <div className="active-agent">
                  {!session.exited && (
                    <>
                      <input
                        ref={imageInputRef}
                        type="file"
                        accept="image/*"
                        style={{ display: "none" }}
                        onChange={(ev) => {
                          const file = ev.target.files?.[0];
                          ev.target.value = "";
                          if (file) void attachImageFile(file);
                        }}
                      />
                      <button
                        type="button"
                        className="btn btn-ghost"
                        disabled={inputDisabled || attachBusy}
                        title={inputDisabled
                          ? "Attaching images works while the workshop is open"
                          : "Save a screenshot into your project and insert its path into the prompt"}
                        onClick={() => imageInputRef.current?.click()}
                      >
                        <ImagePlus size={14} aria-hidden="true" /> {attachBusy ? "Attaching…" : "Attach image"}
                      </button>
                      {attached && (
                        <span className="attach-confirm" role="status" aria-live="polite">
                          <img src={attached.url} alt="" className="attach-thumb" />
                          <span>Attached. Now tell the agent what to do with it.</span>
                          <button
                            type="button"
                            className="icon-btn"
                            aria-label="Dismiss"
                            onClick={() => setAttached(null)}
                          >
                            <X size={12} aria-hidden="true" />
                          </button>
                        </span>
                      )}
                    </>
                  )}
                  <span>{session.label} {session.exited ? "has ended" : "is running"}</span>
                  {session.exited && (
                    <button
                      className="primary-btn"
                      disabled={launching !== null}
                      onClick={() => requestAgent(session.agent_id)}
                    >
                      <Rocket size={14} /> Relaunch
                    </button>
                  )}
                  <button
                    className="icon-btn"
                    aria-label={`Close ${session.label}`}
                    title={`Close ${session.label}`}
                    onClick={() => closeSession(session.id)}
                  >
                    <X size={12} />
                  </button>
                </div>
              </div>
            )}

            <div className="terminal-stage">
              {priorSessions.length > 0 && (view === "home" || !session) && (
                <section className="prior-sessions" aria-label="Sessions ended on restart">
                  <h2>Sessions ended when the workshop restarted</h2>
                  {priorSessions.map((prior) => (
                    <article className="prior-session" key={prior.id}>
                      <div>
                        <strong>{prior.label}</strong>
                        {prior.project_name && <span>Build: {prior.project_name}</span>}
                        <span>
                          {prior.exit_reason === "server_restarted"
                            ? "Ended on workshop restart"
                            : prior.exit_reason}
                        </span>
                      </div>
                      <button
                        className="primary-btn"
                        disabled={launching !== null}
                        onClick={() => relaunchPrior(prior)}
                      >
                        <Rocket size={14} /> Relaunch
                      </button>
                    </article>
                  ))}
                </section>
              )}
              {(view === "home" || !session) && (
                <Hero
                  agents={agents}
                  eventName={branding?.event_name ?? ""}
                  workspaceUrl={config?.workspace_url ?? ""}
                  workspaceLinks={config?.shell.workspace_links ?? []}
                  hasSessions={!!session && !session.exited}
                  launching={launching}
                  brief={projectInventory?.active ? null : brief}
                  canEditBrief={wizardAvailable}
                  onLaunch={requestAgent}
                  onIdea={ideaToSession}
                  onEditBrief={openWizard}
                />
              )}
              {session && (
                <TerminalView
                  key={session.id}
                  sessionId={session.id}
                  active={view === "agent"}
                  readOnly={inputDisabled}
                  onExit={finishSession}
                  onGatewayLimit={handleGatewayLimit}
                />
              )}
            </div>
          </fieldset>

          {showNuggets && (
            <NuggetsPane
              inputDisabled={inputDisabled}
              collapsed={nuggetsCollapsed}
              onToggle={() => setNuggetsCollapsed((c) => !c)}
              onCertificate={openCertificate}
              onTryPrompt={ideaToSession}
            />
          )}
        </div>
      )}

      {wizardOpen && !inputDisabled && (
        <Wizard
          agents={agents}
          launching={launching}
          onLaunch={launchFromWizard}
          onOpenAgent={() => { setView("agent"); setWizardOpen(false); }}
          onClose={closeWizard}
          onSaved={setBrief}
          draftKey={config ? `wt-wizard-draft:v1:${config.workspace_url}:${config.user.email}` : undefined}
        />
      )}

      {pendingSwitch && session && !inputDisabled && (
        <AgentSwitchDialog
          currentLabel={session.label}
          requestedLabel={agentLabel(pendingSwitch.agentId)}
          busy={switching}
          error={switchError}
          onCancel={() => {
            setPendingSwitch(null);
            setSwitchError("");
          }}
          onConfirm={confirmSwitch}
        />
      )}

      {certOpen && !inputDisabled && (
        <div className="modal-backdrop" onClick={() => setCertOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>
              <Award size={18} /> Your certificate
            </h2>
            <p>
              We'll generate a personalized PDF with your build stats — agent sessions,
              lines of code, Databricks resources, topics explored. It downloads straight
              to your laptop.
            </p>
            <label className="modal-label" htmlFor="cert-name">
              Name on the certificate
            </label>
            <input
              id="cert-name"
              value={certName}
              onChange={(e) => setCertName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && downloadCertificate()}
              placeholder="Ada Lovelace"
              autoFocus
            />
            <div className="modal-actions">
              <button className="phase-btn" onClick={() => setCertOpen(false)}>
                Cancel
              </button>
              <button
                className="primary-btn"
                disabled={!certName.trim() || certBusy}
                onClick={downloadCertificate}
              >
                <Download size={14} /> {certBusy ? "Generating…" : "Download PDF"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
