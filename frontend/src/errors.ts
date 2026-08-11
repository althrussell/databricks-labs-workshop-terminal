// Turning harness codes into something an attendee can act on.
//
// Omnigent sanitizes its failures into codes that name the call that failed —
// `spec_resolver_failed`, `native_terminal_start_failed` — and then suggests
// reading the runner logs. An attendee at a workshop has no runner logs, no way
// to reach them, and no idea that all three of those codes usually mean one
// thing: their Databricks sign-in went stale.
//
// So nothing here ever shows a code as the message, and nothing ever says "see
// runner logs". Each known failure gets a sentence about what happened, a
// sentence about what still works, and one button that fixes it.

import { ATTENDEE_ERROR_CODES, codeFromMessage, type AttendeeErrorCode } from "./telemetry";

export type RecoveryAction = "recover" | "reload" | "none";

export interface FriendlyError {
  /** The vocabulary entry an operator sees for the same moment. */
  code: AttendeeErrorCode;
  /** What we put on the attendee's screen. Never a bare code. */
  message: string;
  action: RecoveryAction;
  actionLabel: string;
  /** The server's original wording, kept for the diagnostics report. */
  raw: string;
}

const FALLBACKS = "Claude, Codex and Terminal are unaffected.";

const MESSAGES: Partial<
  Record<AttendeeErrorCode, { message: string; action: RecoveryAction; actionLabel: string }>
> = {
  native_terminal_start_failed: {
    message: `Omnigent could not open a terminal because your Databricks sign-in went stale. ${FALLBACKS}`,
    action: "recover",
    actionLabel: "Recover",
  },
  spec_resolver_failed: {
    message: `Omnigent could not start this agent because your Databricks sign-in went stale. ${FALLBACKS}`,
    action: "recover",
    actionLabel: "Recover",
  },
  runner_disconnected: {
    message: `Omnigent lost its connection. Recovering reconnects it. ${FALLBACKS}`,
    action: "recover",
    actionLabel: "Recover",
  },
  obo_stale: {
    message: `Your Databricks sign-in needs refreshing before Omnigent can start. ${FALLBACKS}`,
    action: "recover",
    actionLabel: "Recover",
  },
  credential_unavailable: {
    message:
      "The workshop credential is not available yet. This usually clears within a minute.",
    action: "recover",
    actionLabel: "Retry",
  },
  install_incomplete: {
    message: "This agent is still being installed. It will become available shortly.",
    action: "none",
    actionLabel: "",
  },
  websocket_lost: {
    message: "Lost the live connection to the workshop.",
    action: "reload",
    actionLabel: "Reload",
  },
  turn_failed: {
    message: "The agent could not finish that turn. Ask it again.",
    action: "none",
    actionLabel: "",
  },
};

/** Remove the parts of a server message that point at something unreachable. */
export function stripDeadEnds(message: string): string {
  return (message || "")
    .replace(/\(?\s*see (?:the )?runner logs?\.?\s*\)?/gi, "")
    .replace(/\(?\s*check (?:the )?logs?\.?\s*\)?/gi, "")
    .replace(/\s{2,}/g, " ")
    .trim();
}

/** The banner to show for a raw error, and the one button that resolves it. */
export function friendlyError(
  raw: string,
  fallback: AttendeeErrorCode = "session_start_failed"
): FriendlyError {
  const code = codeFromMessage(raw, fallback);
  const known = MESSAGES[code];
  if (known) {
    return { code, raw, ...known };
  }
  // Unmapped: the server's own wording is usually good, but a bare harness code
  // is not a sentence — say something true rather than echoing it.
  const cleaned = stripDeadEnds(raw);
  const isBareCode = ATTENDEE_ERROR_CODES.some(
    (candidate) => cleaned.toLowerCase() === candidate
  );
  return {
    code,
    raw,
    message:
      cleaned && !isBareCode
        ? cleaned
        : "Something went wrong starting that agent. Try again, or use Terminal.",
    action: "none",
    actionLabel: "",
  };
}
