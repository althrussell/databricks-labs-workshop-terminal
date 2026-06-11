import { useEffect, useState } from "react";
import { Bot, Check, Loader2, Sparkles, SquareTerminal } from "lucide-react";
import { api, AgentInfo } from "../api";

const ICONS: Record<string, typeof Bot> = {
  sparkles: Sparkles,
  bot: Bot,
  terminal: SquareTerminal,
};

const STEP_LABELS: Record<string, string> = {
  node: "Preparing the runtime",
  claude: "Installing Claude Code",
  codex: "Installing Codex",
  databricks: "Authenticating the Databricks CLI",
  skills: "Fetching the latest Databricks skills",
};

interface Props {
  agents: AgentInfo[];
  eventName: string;
  launching: string | null;
  onLaunch: (agentId: string) => void;
}

// The first thing an attendee sees: an invitation, not an empty void.
export default function Hero({ agents, eventName, launching, onLaunch }: Props) {
  const [steps, setSteps] = useState<Record<string, { status: string }> | null>(null);
  const [installing, setInstalling] = useState(false);

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

        <div className="hero-cards">
          {agents.map((agent, i) => {
            const Icon = ICONS[agent.icon] ?? SquareTerminal;
            const busy = launching === agent.id;
            return (
              <button
                key={agent.id}
                className={`hero-card ${agent.id === "claude" ? "hero-card-primary" : ""}`}
                style={{ animationDelay: `${i * 90}ms` }}
                disabled={!agent.ready || busy}
                onClick={() => onLaunch(agent.id)}
              >
                <span className="hero-card-icon">
                  {busy ? <Loader2 size={22} className="spin" /> : <Icon size={22} />}
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

        <div className="hero-hint">
          💡 Insights about what you're building appear on the right as you work.
        </div>
      </div>
    </div>
  );
}
