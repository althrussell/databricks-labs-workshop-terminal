import { useEffect, useState } from "react";
import {
  AlertTriangle,
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

const FAILED_STATUSES = ["error", "degraded"];

const STEP_LABELS: Record<string, string> = {
  node: "Preparing the runtime",
  claude: "Installing Claude Code",
  codex: "Installing Codex",
  databricks: "Authenticating the Databricks CLI",
  skills: "Fetching the latest Databricks skills",
  tmux: "Preparing the session manager",
  omnigent: "Installing Omnigent",
};

/** What a card that is not launchable should say, and why.
 *
 * Three different reasons used to render as one spinner. An attendee cannot
 * tell "thirty more seconds" from "this will never finish", and neither could
 * the operator they eventually asked.
 */
function cardState(agent: AgentInfo): { text: string; kind: string; title?: string } {
  if (agent.ready) return { text: "ready", kind: "is-ready" };
  if (agent.blocked === "operator_demoted") {
    return {
      text: "paused by your host",
      kind: "is-blocked",
      title:
        "Your host has paused this agent for now. Claude, Codex and Terminal are unaffected.",
    };
  }
  if (agent.blocked) {
    return {
      text: "reload to sign in",
      kind: "is-blocked",
      title:
        "Your Databricks sign-in needs refreshing — reload this tab. Claude, Codex and Terminal still work.",
    };
  }
  if (agent.install_error) {
    // Terminal: nothing retries a failed install step, so the spinner this
    // replaces would have run until the attendee gave up and asked.
    return {
      text: "install failed",
      kind: "is-failed",
      title: `${agent.install_error} — tell your host; use Claude, Codex or Terminal meanwhile.`,
    };
  }
  return { text: "installing", kind: "" };
}

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
  const [steps, setSteps] = useState<Record<
    string,
    { status: string; error?: string | null }
  > | null>(null);
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

  // `degraded` counts: it means the step produced something usable without
  // meeting its reviewed contract, and it never becomes complete on its own.
  const failedSteps = Object.entries(steps ?? {}).filter(([, step]) =>
    FAILED_STATUSES.includes(step.status)
  );

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
            const state = cardState(agent);
            return (
              <button
                key={agent.id}
                className={`hero-card ${i === 0 ? "hero-card-primary" : ""} ${
                  agent.ready ? "" : state.kind
                }`}
                style={{ animationDelay: `${i * 90}ms` }}
                disabled={!agent.ready || busy}
                title={state.title}
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
                <span className={`hero-card-state ${state.kind}`}>
                  {state.kind === "is-ready" ? (
                    <span className="dot dot-online" />
                  ) : state.kind === "" ? (
                    <Loader2 size={11} className="spin" />
                  ) : (
                    <span
                      className={`dot ${
                        state.kind === "is-failed" ? "dot-error" : "dot-warn"
                      }`}
                    />
                  )}{" "}
                  {state.text}
                </span>
              </button>
            );
          })}
        </div>

        {/* Kept on screen after the install finishes when a step failed: the
            spinner used to disappear and leave a permanently unready card with
            no explanation anywhere the attendee could see. */}
        {steps && (installing || failedSteps.length > 0) && (
          <div className="hero-boot">
            <div className="hero-boot-title">
              {installing
                ? "Setting up your workshop…"
                : "Setup finished with a problem"}
            </div>
            {Object.entries(STEP_LABELS).map(([key, label]) =>
              steps[key] ? (
                <div key={key} className="hero-boot-step">
                  {steps[key].status === "complete" ? (
                    <Check size={13} className="boot-done" />
                  ) : FAILED_STATUSES.includes(steps[key].status) ? (
                    <AlertTriangle size={13} className="boot-failed" />
                  ) : (
                    <Loader2 size={13} className="spin boot-busy" />
                  )}
                  <span className={steps[key].status === "complete" ? "boot-done-text" : ""}>
                    {label}
                  </span>
                  {FAILED_STATUSES.includes(steps[key].status) && (
                    <span className="hero-boot-error">
                      {steps[key].error || steps[key].status}
                    </span>
                  )}
                </div>
              ) : null
            )}
            {failedSteps.length > 0 && (
              <div className="hero-boot-note">
                Tell your host — this will not fix itself. Everything marked
                ready above still works.
              </div>
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
