import {
  AlertTriangle,
  BarChart3,
  Bot,
  Check,
  Database,
  FolderTree,
  Home,
  Link as LinkIcon,
  Loader2,
  Sparkles,
  SquareTerminal,
  Workflow,
} from "lucide-react";
import { AgentInfo } from "../api";
import omnigentLogo from "../assets/omnigent.svg";

export const ICONS: Record<string, typeof Bot> = {
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

export type SetupSteps = Record<string, { status: string; error?: string | null }>;

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
        "Your host has paused this agent for now. Claude and Codex are unaffected.",
    };
  }
  if (agent.blocked) {
    return {
      text: "reload to sign in",
      kind: "is-blocked",
      title:
        "Your Databricks sign-in needs refreshing — reload this tab. Claude and Codex still work.",
    };
  }
  if (agent.install_error) {
    // Nothing retries a failed install step, so the spinner this
    // replaces would have run until the attendee gave up and asked.
    return {
      text: "install failed",
      kind: "is-failed",
      title: `${agent.install_error} — tell your host; use another available agent meanwhile.`,
    };
  }
  return { text: "installing", kind: "" };
}

interface Props {
  agents: AgentInfo[];
  launching: string | null;
  onLaunch: (agentId: string) => void;
}

/** The agent picker.
 *
 * Shared between the landing page and the wizard's last step. Both are the same
 * decision made at the same moment, so an attendee who skipped the wizard and
 * one who finished it must not be choosing from two subtly different lists —
 * and a card state fixed in one place must not stay broken in the other.
 */
export default function AgentCards({ agents, launching, onLaunch }: Props) {
  return (
    <div className="hero-cards">
      {agents.map((agent, i) => {
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
                  <img
                    src={imageIcon}
                    alt=""
                    className="hero-card-logo"
                    width={22}
                    height={22}
                  />
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
  );
}

/** The install progress list.
 *
 * Travels with the cards: an unready card is only comprehensible next to the
 * step that has not finished, so showing the picker in the wizard without this
 * would reintroduce the unexplained spinner it was written to remove.
 */
export function SetupProgress({
  steps,
  installing,
}: {
  steps: SetupSteps | null;
  installing: boolean;
}) {
  // `degraded` counts: it means the step produced something usable without
  // meeting its reviewed contract, and it never becomes complete on its own.
  const failedSteps = Object.entries(steps ?? {}).filter(([, step]) =>
    FAILED_STATUSES.includes(step.status)
  );
  if (!steps || (!installing && failedSteps.length === 0)) return null;

  return (
    <div className="hero-boot">
      <div className="hero-boot-title">
        {installing ? "Setting up your workshop…" : "Setup finished with a problem"}
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
          Tell your host — this will not fix itself. Everything marked ready above
          still works.
        </div>
      )}
    </div>
  );
}

/** Poll setup status until it settles. Shared by Home and the wizard. */
export const SETUP_POLL_MS = 4000;
