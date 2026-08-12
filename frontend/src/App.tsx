import { useCallback, useEffect, useRef, useState } from "react";
import {
  Award,
  BookOpen,
  Download,
  GraduationCap,
  House,
  Link as LinkIcon,
  MessageSquare,
  Rocket,
  ShieldCheck,
  SquareTerminal,
  X,
} from "lucide-react";
import {
  api,
  AgentInfo,
  AppConfig,
  PriorSessionInfo,
  SessionInfo,
  WizardBrief,
  relaunchAndAcknowledge,
  splitSessionPayload,
} from "./api";
import databricksLogo from "./assets/databricks-logo.svg";
import BannerBar from "./components/BannerBar";
import Hero from "./components/Hero";
import Wizard from "./components/Wizard";
import LaunchBar from "./components/LaunchBar";
import NuggetsPane from "./components/NuggetsPane";
import OperatorPanel from "./components/OperatorPanel";
import SignInNotice from "./components/SignInNotice";
import RaiseHandButton from "./components/RaiseHandButton";
import HelpChatPanel from "./components/HelpChatPanel";
import ToastHost from "./components/ToastHost";
import TerminalView from "./components/TerminalView";
import { bindIdentityRefresh, onAppEvent } from "./events";
import { friendlyError, type RecoveryAction } from "./errors";
import { codeFromMessage, postAttendeeError, reportAttendeeError } from "./telemetry";

// The escape hatch for an attendee facing an empty prompt. Deliberately a real
// build request rather than a greeting: the coach is told to build immediately
// when the first message is concrete, so this lands them on something working
// instead of in a conversation about what they might do.
const STARTER_PROMPT =
  "Build me something real I can show off by the end of the session — " +
  "pick a good example for a Databricks workshop and just go.";

const LINK_ICONS: Record<string, typeof LinkIcon> = {
  "book-open": BookOpen,
  "graduation-cap": GraduationCap,
  rocket: Rocket,
  link: LinkIcon,
};

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [priorSessions, setPriorSessions] = useState<PriorSessionInfo[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [launching, setLaunching] = useState<string | null>(null);
  const [hintSessionId, setHintSessionId] = useState<string | null>(null);
  const [certOpen, setCertOpen] = useState(false);
  const [certName, setCertName] = useState("");
  const [certBusy, setCertBusy] = useState(false);
  const [error, setError] = useState("");
  const [recovering, setRecovering] = useState(false);
  const [connectionLost, setConnectionLost] = useState(false);
  const [nuggetsCollapsed, setNuggetsCollapsed] = useState(false);
  const [helpChatOpen, setHelpChatOpen] = useState(false);
  const [helpUnread, setHelpUnread] = useState(0);
  const [helpRaised, setHelpRaised] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [brief, setBrief] = useState<WizardBrief | null>(null);
  const [sessionsLoaded, setSessionsLoaded] = useState(false);
  const helpChatOpenRef = useRef(false);
  const wizardChecked = useRef(false);
  const [view, setView] = useState<"home" | "terminals" | "operator">(
    location.pathname.startsWith("/operator") ? "operator" : "home"
  );

  const refreshAgents = useCallback(() => {
    api.agents().then((data) => setAgents(data.agents)).catch(() => undefined);
  }, []);

  /* Stable across renders, because the wizard treats a changed prop identity as
   * a reason to re-run its mount effect — and Home re-renders every few seconds
   * while it polls agent install progress. An inline arrow here used to wipe
   * whatever the attendee was typing into the wizard. */
  // The operator's create-time choice. Unknown while config is still loading,
  // which reads as "on": the wizard's own `should_show` is server-gated too, so
  // an optimistic answer here cannot open a modal a disabled run should not have.
  const wizardAvailable = config?.onboarding_wizard.enabled !== false;

  const openWizard = useCallback(() => {
    if (!wizardAvailable) return;
    setWizardOpen(true);
  }, [wizardAvailable]);

  const closeWizard = useCallback(() => {
    setWizardOpen(false);
    // Pick up whatever the wizard saved so Home's recap line is right
    // immediately, rather than after the next reload.
    api.wizard().then((s) => setBrief(s.brief)).catch(() => undefined);
  }, []);

  const refreshIdentity = useCallback(async () => {
    const cfg = await api.config();
    setConfig(cfg);
    if (cfg.branding.brand_primary_color) {
      document.documentElement.style.setProperty(
        "--brand-primary",
        cfg.branding.brand_primary_color
      );
    }
    document.title = cfg.branding.event_name || `${cfg.branding.brand_name} Workshop`;
  }, []);

  useEffect(() => {
    const refresh = () =>
      refreshIdentity().catch((e) =>
        setError(e instanceof Error ? e.message : String(e))
      );
    refresh();
    const stopIdentityRefresh = bindIdentityRefresh(refresh);

    api.sessions()
      .then((data) => {
        const { live, prior } = splitSessionPayload(data);
        setSessions(live);
        setPriorSessions(prior);
        if (live.length > 0) setActiveId(live[0].id);
      })
      .catch((e) => setError(
        e instanceof Error ? e.message : String(e)
      ))
      // Either way the answer is known, which is what the wizard is waiting on.
      // A failed lookup counts as loaded: an attendee whose very first request
      // failed is not mid-build, and suppressing the wizard forever on a network
      // blip is the worse of the two mistakes.
      .finally(() => setSessionsLoaded(true));

    refreshAgents();
    const interval = setInterval(refreshAgents, 5000);
    return () => {
      clearInterval(interval);
      stopIdentityRefresh();
    };
  }, [refreshAgents, refreshIdentity]);

  useEffect(() => {
    helpChatOpenRef.current = helpChatOpen;
  }, [helpChatOpen]);

  /* The wizard opens once, on the first arrival, and never again.
   *
   * `should_show` is the server's answer, not the browser's: it is keyed on the
   * brief file rather than localStorage so a reload, a second tab, or the
   * reconnect after a wifi flap cannot re-present a modal someone already
   * skipped. In a workshop room all three of those happen, usually to the person
   * least able to shrug it off.
   *
   * Suppressed when a session already exists — a returning attendee is mid-build,
   * and a modal asking what they intend to build is at best late. That check has
   * to wait for the session list to arrive: asking on mount only ever reads the
   * empty array the state starts as, which is indistinguishable from a first
   * arrival and opens the modal over somebody's running terminal. */
  useEffect(() => {
    if (!sessionsLoaded || wizardChecked.current) return;
    // Once per load. Without the guard the wizard would reopen the moment an
    // attendee closed their last terminal, which is precisely when they are
    // least in the mood for it.
    wizardChecked.current = true;
    api
      .wizard()
      .then((state) => {
        setBrief(state.brief);
        if (state.enabled && state.should_show && sessions.length === 0) {
          setWizardOpen(true);
        }
      })
      .catch(() => undefined);
  }, [sessionsLoaded, sessions.length]);

  useEffect(() => {
    if (config?.help) setHelpRaised(config.help.raised);
  }, [config?.help]);

  useEffect(
    () =>
      onAppEvent((event) => {
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

  async function launch(agentId: string, retried = false): Promise<SessionInfo | null> {
    setLaunching(agentId);
    setError("");
    try {
      const { session } = await api.createSession(agentId);
      setSessions((prev) => [...prev, session]);
      setActiveId(session.id);
      setView("terminals");
      if (agentId !== "bash") setHintSessionId(session.id);
      return session;
    } catch (e) {
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
      if (canRetry && !retried) return launch(agentId, true);
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

  // Home-screen cards: an attendee clicking the agent they already have open
  // wants to get back to it, not open a duplicate — and at the session cap a
  // failed launch from Home would otherwise leave no way back to the tabs.
  function openOrLaunch(agentId: string) {
    const existing = sessions.find((s) => s.agent_id === agentId && !s.exited);
    if (existing) {
      setActiveId(existing.id);
      setView("terminals");
      return;
    }
    launch(agentId);
  }

  /** Launch from the wizard's last step, carrying the first prompt in.
   *
   * Deliberately the same typing mechanism the ideation chips use: the prompt
   * lands at the agent's prompt UNSENT so the attendee presses Enter. The wizard
   * hands them a loaded first move; it does not take the move for them, and an
   * agent that started talking on its own would undercut the whole point of
   * putting them in front of a terminal.
   */
  async function launchFromWizard(agentId: string, starterPrompt: string) {
    setWizardOpen(false);
    const session = await launch(agentId);
    if (!session || !starterPrompt) return;
    try {
      // The CLI needs a moment before text will land at its prompt.
      await new Promise((r) => setTimeout(r, 4000));
      await api.typeIntoSession(session.id, starterPrompt);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  // Ideation chips / insight-card prompts: type the text into the attendee's
  // agent session UNSENT — they press Enter. Opens a Claude session if none.
  async function ideaToSession(prompt: string) {
    let target =
      sessions.find((s) => s.agent_id === "claude" && !s.exited) ??
      sessions.find((s) => !s.exited);
    let fresh = false;
    if (!target) {
      target = (await launch("claude")) ?? undefined;
      fresh = true;
      if (!target) return;
    } else {
      setActiveId(target.id);
      setView("terminals");
    }
    try {
      if (fresh) {
        // Give the CLI a moment to boot before the text lands at its prompt.
        await new Promise((r) => setTimeout(r, 4000));
      }
      await api.typeIntoSession(target.id, prompt);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function closeSession(id: string) {
    try {
      await api.closeSession(id);
    } catch {
      /* already gone server-side */
    }
    removeSession(id);
  }

  const removeSession = useCallback((id: string) => {
    setSessions((prev) => {
      const next = prev.filter((s) => s.id !== id);
      setActiveId((current) => (current === id ? next[0]?.id ?? null : current));
      return next;
    });
  }, []);

  function openCertificate() {
    if (!certName && config) {
      // Lab identities are generic — prefill a readable guess, let them fix it.
      const local = config.user.email.split("@")[0].replace(/[._+-]+/g, " ").trim();
      setCertName(local.replace(/\b\w/g, (ch) => ch.toUpperCase()));
    }
    setCertOpen(true);
  }

  async function downloadCertificate() {
    const name = certName.trim();
    if (!name) return;
    setCertBusy(true);
    try {
      const resp = await fetch(`/api/certificate?name=${encodeURIComponent(name)}`);
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

  if (!config && !error) {
    return <div className="boot-screen">Loading workshop…</div>;
  }

  const branding = config?.branding;
  const shellLinks = config?.shell.links ?? [];
  const showNuggets = config?.shell.features.nuggets_pane !== false;
  const isAdmin = config?.user.is_admin ?? false;

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
        <nav className="header-links">
          {/* 1. Navigation (where am I) */}
          <button
            className={`operator-toggle ${view === "home" ? "operator-toggle-active" : ""}`}
            onClick={() => setView("home")}
            title="Back to Home — your sessions keep running"
          >
            <House size={14} />
            Home
          </button>
          {sessions.length > 0 && (
            <button
              className={`operator-toggle ${view === "terminals" ? "operator-toggle-active" : ""}`}
              onClick={() => setView("terminals")}
              title="Back to your open terminals"
            >
              <SquareTerminal size={14} />
              Terminals ({sessions.length})
            </button>
          )}
          {isAdmin && (
            <button
              className={`operator-toggle ${view === "operator" ? "operator-toggle-active" : ""}`}
              onClick={() => setView(view === "operator" ? "terminals" : "operator")}
            >
              <ShieldCheck size={14} />
              Operator
            </button>
          )}
          <span className="nav-divider" />
          {/* 2. Resources (reference links) */}
          {shellLinks
            .filter((link) => !link.highlight)
            .map((link) => {
              const Icon = LINK_ICONS[link.icon] ?? LinkIcon;
              return (
                <a key={link.url} href={link.url} target="_blank" rel="noreferrer">
                  <Icon size={14} />
                  {link.label}
                </a>
              );
            })}
          {config?.workspace_url && (
            <a
              href={config.workspace_url}
              target="_blank"
              rel="noopener noreferrer"
              title="Open the Databricks workspace"
            >
              <LinkIcon size={14} />
              Open Workspace
            </a>
          )}
          {config?.omnigent_remote?.enabled && config.omnigent_remote.url && (
            <a
              href={config.omnigent_remote.url}
              target="_blank"
              rel="noopener noreferrer"
              title="Open the dedicated Omnigent app"
            >
              <Rocket size={14} />
              Open Omnigent
            </a>
          )}
          {/* 3. Actions, then the promoted CTA at the far right */}
          <button className="operator-toggle" onClick={openCertificate} title="Download your certificate">
            <Award size={14} />
            Certificate
          </button>
          <RaiseHandButton
            initial={config?.help ?? null}
            raised={helpRaised}
            onRaisedChange={setHelpRaised}
          />
          <button
            type="button"
            className="help-chat-toggle"
            onClick={() => setHelpChatOpen(true)}
            title="Ask operators"
          >
            <MessageSquare size={14} />
            Help
            {helpUnread > 0 ? <span className="help-chat-badge">{helpUnread}</span> : null}
          </button>
          {shellLinks
            .filter((link) => link.highlight)
            .map((link) => {
              const Icon = LINK_ICONS[link.icon] ?? LinkIcon;
              return (
                <a key={link.url} href={link.url} target="_blank" rel="noreferrer" className="link-highlight">
                  <Icon size={14} />
                  {link.label}
                </a>
              );
            })}
        </nav>
        <div className="header-user">{config?.user.email}</div>
      </header>

      <BannerBar initial={config?.broadcast ?? null} />
      {/* The tab rule. Above the other banners because a stale sign-in is the
          cause of most of what they warn about. */}
      <SignInNotice
        omnigentEnabled={!!config?.omnigent_remote.enabled}
        obo={config?.obo}
        onReload={() => location.reload()}
      />
      <ToastHost
        onOpenHelp={() => setHelpChatOpen(true)}
        helpOpen={helpChatOpen}
      />
      <HelpChatPanel
        open={helpChatOpen}
        onClose={() => setHelpChatOpen(false)}
        unread={helpUnread}
        onClearUnread={() => setHelpUnread(0)}
        raised={helpRaised}
        onRaisedChange={setHelpRaised}
      />
      {connectionLost && (
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
            provided a valid app-identity OAuth bearer. Plain terminals work; coding
            agents can't authenticate yet.
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
      {error && (
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
          <div className="work-area">
            {sessions.length > 0 && view === "terminals" && (
            <div className="toolbar">
              <LaunchBar agents={agents} launching={launching} onLaunch={launch} />
              <div className="tabs">
                {sessions.map((session) => (
                  <div
                    key={session.id}
                    className={`tab ${session.id === activeId ? "tab-active" : ""}`}
                    onClick={() => setActiveId(session.id)}
                  >
                    <span>{session.label}</span>
                    <button
                      className="icon-btn"
                      onClick={(e) => {
                        e.stopPropagation();
                        closeSession(session.id);
                      }}
                    >
                      <X size={12} />
                    </button>
                  </div>
                ))}
              </div>
            </div>
            )}

            {hintSessionId && hintSessionId === activeId && (
              <div className="coach-hint">
                <span>
                  👋 Your coach is ready — tell it what you'd like to build.
                </span>
                <button
                  className="coach-hint-action"
                  onClick={() => {
                    setHintSessionId(null);
                    ideaToSession(STARTER_PROMPT);
                  }}
                >
                  Not sure? Start me off
                </button>
                <button className="icon-btn" onClick={() => setHintSessionId(null)}>
                  <X size={12} />
                </button>
              </div>
            )}
            <div className="terminal-stage">
              {priorSessions.length > 0 && (view === "home" || sessions.length === 0) && (
                <section className="prior-sessions" aria-label="Sessions ended on restart">
                  <h2>Sessions ended when the workshop restarted</h2>
                  {priorSessions.map((prior) => (
                    <article className="prior-session" key={prior.id}>
                      <div>
                        <strong>{prior.label}</strong>
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
              {(view === "home" || sessions.length === 0) && (
                <Hero
                  agents={agents}
                  eventName={branding?.event_name ?? ""}
                  workspaceUrl={config?.workspace_url ?? ""}
                  workspaceLinks={config?.shell.workspace_links ?? []}
                  hasSessions={sessions.length > 0}
                  launching={launching}
                  brief={brief}
                  canEditBrief={wizardAvailable}
                  onLaunch={openOrLaunch}
                  onIdea={ideaToSession}
                  onEditBrief={openWizard}
                />
              )}
              {sessions.map((session) => (
                <TerminalView
                  key={session.id}
                  sessionId={session.id}
                  active={view === "terminals" && session.id === activeId}
                  onExit={removeSession}
                />
              ))}
            </div>
          </div>

          {showNuggets && (
            <NuggetsPane
              collapsed={nuggetsCollapsed}
              onToggle={() => setNuggetsCollapsed((c) => !c)}
              onCertificate={openCertificate}
              onTryPrompt={ideaToSession}
            />
          )}
        </div>
      )}

      {wizardOpen && (
        <Wizard
          agents={agents}
          launching={launching}
          onLaunch={launchFromWizard}
          onClose={closeWizard}
        />
      )}

      {certOpen && (
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

