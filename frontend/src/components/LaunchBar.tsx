import { Bot, Loader2, Sparkles, SquareTerminal } from "lucide-react";
import { AgentInfo } from "../api";
import omnigentLogo from "../assets/omnigent.png";

const ICONS: Record<string, typeof Bot> = {
  sparkles: Sparkles,
  bot: Bot,
  terminal: SquareTerminal,
};

const IMAGE_ICONS: Record<string, string> = {
  omnigent: omnigentLogo,
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
        const imageIcon = IMAGE_ICONS[agent.icon];
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
            ) : imageIcon ? (
              <img src={imageIcon} alt="" width={16} height={16} />
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
