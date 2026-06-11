import { useCallback, useEffect, useState } from "react";
import {
  BookOpen,
  GraduationCap,
  Link as LinkIcon,
  Plus,
  ShieldCheck,
  X,
} from "lucide-react";
import { api, AgentInfo, AppConfig, SessionInfo } from "./api";
import databricksLogo from "./assets/databricks-logo.svg";
import BannerBar from "./components/BannerBar";
import LaunchBar from "./components/LaunchBar";
import NuggetsPane from "./components/NuggetsPane";
import OperatorPanel from "./components/OperatorPanel";
import TerminalView from "./components/TerminalView";

const LINK_ICONS: Record<string, typeof LinkIcon> = {
  "book-open": BookOpen,
  "graduation-cap": GraduationCap,
  link: LinkIcon,
};

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [launching, setLaunching] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [nuggetsCollapsed, setNuggetsCollapsed] = useState(false);
  const [view, setView] = useState<"terminals" | "operator">(
    location.pathname.startsWith("/operator") ? "operator" : "terminals"
  );

  const refreshAgents = useCallback(() => {
    api.agents().then((data) => setAgents(data.agents)).catch(() => undefined);
  }, []);

  useEffect(() => {
    api
      .config()
      .then((cfg) => {
        setConfig(cfg);
        if (cfg.branding.brand_primary_color) {
          document.documentElement.style.setProperty(
            "--brand-primary",
            cfg.branding.brand_primary_color
          );
        }
        document.title = cfg.branding.event_name || `${cfg.branding.brand_name} Workshop`;
      })
      .catch((e) => setError(e.message));

    api.sessions().then((data) => {
      setSessions(data.sessions);
      if (data.sessions.length > 0) setActiveId(data.sessions[0].id);
    });

    refreshAgents();
    const interval = setInterval(refreshAgents, 5000);
    return () => clearInterval(interval);
  }, [refreshAgents]);

  // Stop polling agents once everything is installed.
  const allReady = agents.length > 0 && agents.every((a) => a.ready);
  useEffect(() => {
    if (allReady) refreshAgents();
  }, [allReady, refreshAgents]);

  async function launch(agentId: string) {
    setLaunching(agentId);
    setError("");
    try {
      const { session } = await api.createSession(agentId);
      setSessions((prev) => [...prev, session]);
      setActiveId(session.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLaunching(null);
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
          {shellLinks.map((link) => {
            const Icon = LINK_ICONS[link.icon] ?? LinkIcon;
            return (
              <a key={link.url} href={link.url} target="_blank" rel="noreferrer">
                <Icon size={14} />
                {link.label}
              </a>
            );
          })}
          {isAdmin && (
            <button
              className={`operator-toggle ${view === "operator" ? "operator-toggle-active" : ""}`}
              onClick={() => setView(view === "operator" ? "terminals" : "operator")}
            >
              <ShieldCheck size={14} />
              Operator
            </button>
          )}
        </nav>
        <div className="header-user">{config?.user.email}</div>
      </header>

      <BannerBar initial={config?.broadcast ?? null} />
      {config && !config.credential.configured && (
        <div className="banner banner-warning">
          <span>
            Workshop credential not configured — Control Tower must inject WORKSHOP_PAT at
            deploy time. Plain terminals work; coding agents can't authenticate yet.
          </span>
        </div>
      )}
      {error && (
        <div className="banner banner-warning">
          <span>{error}</span>
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

            <div className="terminal-stage">
              {sessions.length === 0 && (
                <div className="empty-state">
                  <Plus size={28} />
                  <h2>Launch your first terminal</h2>
                  <p>
                    Pick <strong>Claude Code</strong> or <strong>Codex</strong> above — your
                    Databricks credentials are wired up automatically. Nothing to paste,
                    nothing to configure.
                  </p>
                </div>
              )}
              {sessions.map((session) => (
                <TerminalView
                  key={session.id}
                  sessionId={session.id}
                  active={session.id === activeId}
                  onExit={removeSession}
                />
              ))}
            </div>
          </div>

          {showNuggets && (
            <NuggetsPane
              collapsed={nuggetsCollapsed}
              onToggle={() => setNuggetsCollapsed((c) => !c)}
            />
          )}
        </div>
      )}
    </div>
  );
}

