import { useEffect, useState } from "react";
import { Lightbulb, Link as LinkIcon, Pencil, Wand2 } from "lucide-react";
import { api, AgentInfo, IdeaPrompt, Nugget, WorkspaceLink, WizardBrief } from "../api";
import { humanIndustry } from "../wizard";
import AgentCards, { ICONS, SetupProgress, SetupSteps, SETUP_POLL_MS } from "./AgentCards";

interface Props {
  agents: AgentInfo[];
  eventName: string;
  workspaceUrl: string;
  workspaceLinks: WorkspaceLink[];
  hasSessions: boolean;
  launching: string | null;
  brief: WizardBrief | null;
  /** False when the operator switched the wizard off: the recap stays, the way
   * back into a modal this workshop does not use does not. */
  canEditBrief: boolean;
  onLaunch: (agentId: string) => void;
  onIdea: (prompt: string) => void;
  onEditBrief: () => void;
}

// Home: the place attendees launch from, get ideas from, and come back to.
export default function Hero({
  agents,
  eventName,
  workspaceUrl,
  workspaceLinks,
  hasSessions,
  launching,
  brief,
  canEditBrief,
  onLaunch,
  onIdea,
  onEditBrief,
}: Props) {
  const [steps, setSteps] = useState<SetupSteps | null>(null);
  const [installing, setInstalling] = useState(false);
  const [ideas, setIdeas] = useState<IdeaPrompt[]>([]);
  const [tips, setTips] = useState<Nugget[]>([]);

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
    timer = setInterval(poll, SETUP_POLL_MS);
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

  // What they told the wizard, if they told it anything. Shown so the answer is
  // visible and changeable rather than something that vanished into a modal —
  // an attendee whose plan changed an hour in has nowhere else to say so.
  const briefLine = brief && !brief.skipped ? brief.what_building.trim() : "";
  const briefIndustry =
    brief && !brief.skipped && brief.industry_stated ? brief.industry : "";

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

        {briefLine && (
          <div className="hero-brief">
            <span className="hero-brief-label">You're building</span>
            <span className="hero-brief-text">{briefLine}</span>
            {briefIndustry && (
              <span className="hero-brief-industry">
                {humanIndustry(briefIndustry)}
              </span>
            )}
            {canEditBrief && (
              <button className="hero-brief-edit" onClick={onEditBrief}>
                <Pencil size={11} /> Change what I'm building
              </button>
            )}
          </div>
        )}

        <AgentCards agents={agents} launching={launching} onLaunch={onLaunch} />

        {/* Kept on screen after the install finishes when a step failed: the
            spinner used to disappear and leave a permanently unready card with
            no explanation anywhere the attendee could see. */}
        <SetupProgress steps={steps} installing={installing} />

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
