/** Exit metadata contains no terminal output or harness-provided error prose. */
export interface TerminalExit {
  reason?: string;
  exit_code?: number | null;
  exit_signal?: number | null;
}

export function terminalExitNotice(exit: TerminalExit): string {
  if (Number.isInteger(exit.exit_signal) && (exit.exit_signal ?? 0) > 0) {
    return `Session stopped by signal ${exit.exit_signal}. Scroll up for details. Select Relaunch to retry.`;
  }
  if (Number.isInteger(exit.exit_code) && exit.exit_code !== 0) {
    return `Session ended with exit code ${exit.exit_code}. Scroll up for details. Select Relaunch to retry.`;
  }
  if (exit.reason === "process_error" || exit.reason === "process_signal") {
    return "Session stopped unexpectedly. Scroll up for details. Select Relaunch to retry.";
  }
  if (exit.reason === "idle_reaped") {
    return "Session ended after being idle. Select Relaunch to continue.";
  }
  return "Session ended. Select Relaunch to start again.";
}
