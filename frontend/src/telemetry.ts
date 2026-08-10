// What the attendee actually saw, reported back so an operator can see it too.
//
// The server can observe almost everything except the one fact that matters
// most during an incident: what ended up on the screen. A launch that fails
// server-side is already an event; a banner the attendee stares at for five
// minutes before saying anything is not. This closes that gap.
//
// Fire-and-forget by contract. Nothing in the UI may wait on it, and a failed
// report is never surfaced — an attendee who is already looking at an error
// should not then be told that reporting the error failed.

/** The fixed vocabulary the server aggregates on. Anything else is `unknown`. */
export const ATTENDEE_ERROR_CODES = [
  "native_terminal_start_failed",
  "spec_resolver_failed",
  "runner_disconnected",
  "turn_failed",
  "session_start_failed",
  "credential_unavailable",
  "obo_stale",
  "websocket_lost",
  "install_incomplete",
] as const;

export type AttendeeErrorCode = (typeof ATTENDEE_ERROR_CODES)[number];

/** Recognise a known failure inside a message the attendee is being shown.
 *
 * Codes leak into user-facing text through Omnigent's sanitized errors, which
 * arrive as prose. Matching them here means the attendee's report and the
 * collector's journal entry carry the same string, so the two can be joined.
 */
export function codeFromMessage(
  message: string,
  fallback: AttendeeErrorCode
): AttendeeErrorCode {
  const text = (message || "").toLowerCase();
  const match = ATTENDEE_ERROR_CODES.find((code) => text.includes(code));
  if (match) return match;
  if (text.includes("still installing")) return "install_incomplete";
  if (text.includes("credential")) return "credential_unavailable";
  return fallback;
}

export function reportAttendeeError(
  code: AttendeeErrorCode,
  detail = "",
  extra: { agentId?: string; sessionId?: string } = {}
): void {
  void postAttendeeError(code, detail, extra);
}

/** Report, and answer the only question the caller cares about: try again?
 *
 * The server self-heals credential failures the moment it hears about one, so
 * for the auth family the honest answer is often "yes, and it will work now".
 * Resolves false on any error — a report that failed cannot promise anything.
 */
export async function postAttendeeError(
  code: AttendeeErrorCode,
  detail = "",
  extra: { agentId?: string; sessionId?: string } = {}
): Promise<boolean> {
  try {
    const response = await fetch("/api/telemetry/error", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        code,
        detail: detail.slice(0, 300),
        agent_id: extra.agentId ?? "",
        session_id: extra.sessionId ?? "",
      }),
      keepalive: true,
    });
    if (!response.ok) return false;
    const body = (await response.json()) as { retry?: boolean };
    return body.retry === true;
  } catch {
    /* reporting an error must never raise one */
    return false;
  }
}
