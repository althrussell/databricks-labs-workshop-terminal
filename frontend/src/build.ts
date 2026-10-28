/** "Your build" (W4): what the attendee sees about their own project. */

export type BuildStatus = "not_started" | "needs_decision" | "ready_to_try" | "blocked" | "help_requested" | "working";

export interface BuildReview {
  state: "verified" | "stale" | "unverified" | "none";
  latest?: string | null;
  gaps?: string[];
}

export interface BuildView {
  project: string | null;
  goal: string;
  phase: string;
  path: "rapid" | "guided" | null;
  status: BuildStatus;
  next_decision: string;
  /** The next step in the attendee's own terms; next_decision is the agent's. */
  attendee_next?: string;
  direction?: { text: string; confirmed_by: string | null } | null;
  open_questions: { id: string; text: string }[];
  review: BuildReview;
  deploy: { state: "deployed" | "not_deployed"; preview_url: string | null };
  what_changed: string[];
  requests: { id: string; kind: "change" | "explain"; text: string }[];
  session: { active: boolean; agent?: string; label?: string };
  updated_at: string | null;
}

const PHASES: Record<string, string> = {
  understand: "Understanding your goal", choose_path: "Choosing how to work", shape: "Shaping the idea",
  agree: "Agreeing a direction", build: "Building", inspect: "Checking it works", improve: "Improving",
  feedback: "Your turn to try it",
};

export function phaseLabel(phase: string): string {
  return PHASES[phase] ?? "Getting started";
}

export function reviewLabel(review: BuildReview): string {
  switch (review.state) {
    case "verified": return "Checked on screen";
    case "stale": return "Changed since it was last checked";
    case "unverified": return `Not verified${review.gaps?.length ? `: ${review.gaps[review.gaps.length - 1]}` : ""}`;
    default: return "Not checked yet";
  }
}

/** Which actions make sense now. Nothing is offered that the server cannot honour. */
export function availableActions(view: BuildView) {
  const hasBuild = Boolean(view.project);
  return {
    tryApp: Boolean(view.deploy.preview_url),
    changeDirection: hasBuild,
    explain: hasBuild,
    stop: view.session.active,
    confirmDirection: view.path === "guided" && Boolean(view.direction?.text) && view.direction?.confirmed_by !== "attendee",
    choosePath: hasBuild && !view.path,
    exportCode: hasBuild,
  };
}
