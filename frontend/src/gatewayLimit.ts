import type { AttendeeErrorCode } from "./telemetry";

const ANSI_ESCAPE = /\x1b(?:\[[0-?]*[ -\/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))/g;

/** Classify explicit gateway failures from a small terminal-output window.
 *
 * Claude and Codex own the terminal UI, so their model HTTP failures do not
 * travel through Workshop Terminal's API client. The browser can still explain
 * a definitive budget denial immediately without storing or sending terminal
 * contents anywhere.
 */
export function terminalGatewayLimit(
  output: string
): Extract<AttendeeErrorCode, "gateway_allowance_exhausted" | "gateway_rate_limited"> | null {
  const text = output.replace(ANSI_ESCAPE, "").toLowerCase();
  const failure = "(?:\\b403\\b|\\b429\\b|forbidden|too many requests)";
  const allowance = [
    "budget",
    "spend[ _-]limit",
    "allowance",
    "quota[ _-]exhausted",
    "cost[ _-]limit",
    "usage[ _-]limit",
    "out[ _-]of[ _-]credits",
    "exceeded[ _-]limit",
  ].join("|");
  const rateLimit = [
    "rate[ _-]limit",
    "too many requests",
    "tpm[ _-]exceeded",
    "rpm[ _-]exceeded",
  ].join("|");
  // Prefer an explicit request/token rate-limit denial over generic allowance
  // words. The rolling terminal window can contain ordinary project prose
  // such as "budget" immediately before a definitive 429 TPM/RPM error.
  if (text.includes("too many requests")) return "gateway_rate_limited";
  const rateLimitFailure = new RegExp(
    `(?:${failure})[\\s\\S]{0,768}(?:${rateLimit})|(?:${rateLimit})[\\s\\S]{0,768}(?:${failure})`
  );
  if (rateLimitFailure.test(text)) return "gateway_rate_limited";

  // Require the HTTP denial and allowance wording to be part of the same
  // nearby error. A project can legitimately print both words at different
  // times; that must not become a workshop budget banner.
  const allowanceFailure = new RegExp(
    `(?:${failure})[\\s\\S]{0,768}(?:${allowance})|(?:${allowance})[\\s\\S]{0,768}(?:${failure})`
  );
  if (allowanceFailure.test(text)) return "gateway_allowance_exhausted";
  return null;
}
