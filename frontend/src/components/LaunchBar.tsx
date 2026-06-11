import { Bot, Loader2, Sparkles, SquareTerminal } from "lucide-react";
import { AgentInfo } from "../api";

const ICONS: Record<string, typeof Bot> = {
  sparkles: Sparkles,
  bot: Bot,
  terminal: SquareTerminal,
};

interface Props {
  agents: AgentInfo[];
  launching: string | null;
  onLaunch: (agentId: string) => void;
}

export default function LaunchBar({ agents, launching, onLaunch }: Props) {
  return (
    <div className="launch-bar">
      {agents.map((agent) => {
        const Icon = ICONS[agent.icon] ?? SquareTerminal;
        const busy = launching === agent.id;
        return (
          <button
            key={agent.id}
            className={`launch-btn ${agent.id === "bash" ? "launch-btn-plain" : ""}`}
            disabled={!agent.ready || busy}
            title={agent.ready ? agent.description : `${agent.label} is installing…`}
            onClick={() => onLaunch(agent.id)}
          >
            {busy || !agent.ready ? (
              <Loader2 size={16} className="spin" />
            ) : (
              <Icon size={16} />
            )}
            <span>{agent.label}</span>
            {!agent.ready && <span className="launch-installing">installing…</span>}
          </button>
        );
      })}
    </div>
  );
}
