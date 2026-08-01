import { useEffect, useState } from "react";
import {
  BarChart3,
  Bot,
  Check,
  Database,
  FolderTree,
  Home,
  Lightbulb,
  Link as LinkIcon,
  Loader2,
  Sparkles,
  SquareTerminal,
  Wand2,
  Workflow,
} from "lucide-react";
import { api, AgentInfo, IdeaPrompt, Nugget, Persona, WorkspaceLink } from "../api";
import omnigentLogo from "../assets/omnigent.svg";

const ICONS: Record<string, typeof Bot> = {
  sparkles: Sparkles,
  bot: Bot,
  terminal: SquareTerminal,
  database: Database,
  "folder-tree": FolderTree,
  "bar-chart-3": BarChart3,
  workflow: Workflow,
  home: Home,
  link: LinkIcon,
};

// Catalog icons that are bundled images rather than lucide glyph names.
const IMAGE_ICONS: Record<string, string> = {
  omnigent: omnigentLogo,
};

const STEP_LABELS: Record<string, string> = {
  node: "Preparing the runtime",
  claude: "Installing Claude Code",
  codex: "Installing Codex",
  databricks: "Authenticating the Databricks CLI",
  skills: "Fetching the latest Databricks skills",
  tmux: "Preparing the session manager",
  omnigent: "Installing Omnigent",
};

interface Props {
  agents: AgentInfo[];
  eventName: string;
  workspaceUrl: string;
  workspaceLinks: WorkspaceLink[];
  hasSessions: boolean;
  launching: string | null;
  onLaunch: (agentId: string) => void;
  onIdea: (prompt: string) => void;
}

// Home: the place attendees launch from, get ideas from, and come back to.
export default function Hero({
  agents,
  eventName,
  workspaceUrl,
  workspaceLinks,
  hasSessions,
  launching,
  onLaunch,
  onIdea,
}: Props) {
  const [steps, setSteps] = useState<Record<string, { status: string }> | null>(null);
  const [installing, setInstalling] = useState(false);
  const [ideas, setIdeas] = useState<IdeaPrompt[]>([]);
  const [tips, setTips] = useState<Nugget[]>([]);
  const [persona, setPersona] = useState<Persona | null>(null);

  // Asked here, while they are still reading the page, so the agent never has
  // to spend the first turn asking it. Optional on purpose: the server assumes
  // plain language when nobody picks, which is the safer of the two guesses.
  function choosePersona(next: Persona) {
    setPersona(next);
    api.setPersona(next).catch(() => setPersona(null));
  }

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    const poll = () =>
      api
        .setupStatus()
        .then((s) => {
          setSteps(s.steps);
          setInstalling(s.installing);
          if (!s.installing && timer) clearInterval(timer);
        })
        .catch(() => undefined);
    poll();
    timer = setInterval(poll, 4000);
    return () => {
      if (timer) clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    const refresh = () =>
      api
        .nuggets()
        .then((data) => {
          setIdeas(data.prompts);
          setTips(data.nuggets.filter((n) => !n.nudge).slice(0, 2));
        })
        .catch(() => undefined);
    refresh();
    const timer = setInterval(refresh, 60000);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="hero">
      <div className="hero-glow hero-glow-a" />
      <div className="hero-glow hero-glow-b" />
      <div className="hero-inner">
        {eventName && <div className="hero-eyebrow">{eventName}</div>}
        <h1 className="hero-title">
          What will you <span className="hero-accent">build</span> today?
        </h1>
        <p className="hero-sub">
          Pick your AI coding agent. Credentials, skills, and the Databricks CLI are
          already wired up — nothing to paste, nothing to configure.
        </p>

        <div className="hero-persona">
          <span className="hero-persona-label">How should it explain things?</span>
          <div className="hero-persona-options">
            <button
              className={`hero-persona-btn ${
                persona === "business" ? "hero-persona-btn-active" : ""
              }`}
              onClick={() => choosePersona("business")}
            >
              Plain language
            </button>
            <button
              className={`hero-persona-btn ${
                persona === "technical" ? "hero-persona-btn-active" : ""
              }`}
              onClick={() => choosePersona("technical")}
            >
              I'm technical
            </button>
          </div>
        </div>

        <div className="hero-cards">
          {/* The home screen sells the AI agents; the plain bash terminal
              stays reachable from the in-session LaunchBar toolbar. */}
          {agents.filter((agent) => agent.id !== "bash").map((agent, i) => {
            const Icon = ICONS[agent.icon] ?? SquareTerminal;
            const imageIcon = IMAGE_ICONS[agent.icon];
            const busy = launching === agent.id;
            return (
              <button
                key={agent.id}
                className={`hero-card ${i === 0 ? "hero-card-primary" : ""}`}
                style={{ animationDelay: `${i * 90}ms` }}
                disabled={!agent.ready || busy}
                onClick={() => onLaunch(agent.id)}
              >
                <span className="hero-card-icon">
                  {busy ? (
                    <Loader2 size={22} className="spin" />
                  ) : imageIcon ? (
                    <img src={imageIcon} alt="" className="hero-card-logo" width={22} height={22} />
                  ) : (
                    <Icon size={22} />
                  )}
                </span>
                <span className="hero-card-label">{agent.label}</span>
                <span className="hero-card-desc">{agent.description}</span>
                <span className={`hero-card-state ${agent.ready ? "is-ready" : ""}`}>
                  {agent.ready ? (
                    <>
                      <span className="dot dot-online" /> ready
                    </>
                  ) : (
                    <>
                      <Loader2 size={11} className="spin" /> installing
                    </>
                  )}
                </span>
              </button>
            );
          })}
        </div>

        {installing && steps && (
          <div className="hero-boot">
            <div className="hero-boot-title">Setting up your workshop…</div>
            {Object.entries(STEP_LABELS).map(([key, label]) =>
              steps[key] ? (
                <div key={key} className="hero-boot-step">
                  {steps[key].status === "complete" ? (
                    <Check size={13} className="boot-done" />
                  ) : (
                    <Loader2 size={13} className="spin boot-busy" />
                  )}
                  <span className={steps[key].status === "complete" ? "boot-done-text" : ""}>
                    {label}
                  </span>
                </div>
              ) : null
            )}
          </div>
        )}

        {ideas.length > 0 && (
          <div className="hero-section">
            <div className="hero-section-title">
              <Wand2 size={13} /> Need an idea? One click types it into your agent
            </div>
            <div className="hero-chips">
              {ideas.map((idea) => (
                <button
                  key={idea.label}
                  className="hero-chip"
                  title={idea.prompt}
                  onClick={() => onIdea(idea.prompt)}
                >
                  {idea.label}
                </button>
              ))}
            </div>
          </div>
        )}

        {workspaceUrl && workspaceLinks.length > 0 && (
          <div className="hero-section">
            <div className="hero-section-title">
              <LinkIcon size={13} /> Dive into your workspace
            </div>
            <div className="hero-tiles">
              {workspaceLinks.map((link) => {
                const Icon = ICONS[link.icon] ?? LinkIcon;
                return (
                  <a
                    key={link.label}
                    className="hero-tile"
                    href={`${workspaceUrl}${link.path}`}
                    target="_blank"
                    rel="noreferrer"
                    title={link.description}
                  >
                    <Icon size={15} />
                    <span>{link.label}</span>
                  </a>
                );
              })}
            </div>
          </div>
        )}

        {tips.length > 0 && (
          <div className="hero-tips">
            {tips.map((tip) => (
              <span key={tip.id} className="hero-tip">
                <Lightbulb size={12} /> <strong>{tip.title}</strong>
              </span>
            ))}
          </div>
        )}

        {!hasSessions && (
          <div className="hero-hint">
            💡 Insights about what you're building appear on the right as you work.
          </div>
        )}
      </div>
    </div>
  );
}
