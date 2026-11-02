/** Exit metadata contains no terminal output or harness-provided error prose. */
export interface TerminalExit {
  reason?: string;
  exit_code?: number | null;
  exit_signal?: number | null;
  agent?: string;
}

/** Each CLI keeps its transcript on disk; a relaunch starts blank unless told to resume. */
const RESUME_HINT: Record<string, string> = {
  claude: "Then type /resume to pick up your conversation.",
  codex: "Then run `codex resume` to pick up your conversation.",
};

function resumeHint(exit: TerminalExit): string {
  const hint = typeof exit.agent === "string" ? RESUME_HINT[exit.agent] : undefined;
  return hint ? ` ${hint}` : "";
}

export function terminalExitNotice(exit: TerminalExit): string {
  if (exit.reason === "workshop_closing") {
    return "This workshop is closing. New input is disabled.";
  }
  if (Number.isInteger(exit.exit_signal) && (exit.exit_signal ?? 0) > 0) {
    return `Session stopped by signal ${exit.exit_signal}. Scroll up for details. Select Relaunch to retry.${resumeHint(exit)}`;
  }
  if (Number.isInteger(exit.exit_code) && exit.exit_code !== 0) {
    return `Session ended with exit code ${exit.exit_code}. Scroll up for details. Select Relaunch to retry.${resumeHint(exit)}`;
  }
  if (exit.reason === "process_error" || exit.reason === "process_signal") {
    return `Session stopped unexpectedly. Scroll up for details. Select Relaunch to retry.${resumeHint(exit)}`;
  }
  if (exit.reason === "idle_reaped") {
    return `Session ended after being idle. Select Relaunch to continue.${resumeHint(exit)}`;
  }
  return "Session ended. Select Relaunch to start again.";
}
