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
  const rateLimit = [
    "rate[ _-]limit",
    "too many requests",
    "tpm[ _-]exceeded",
    "rpm[ _-]exceeded",
  ].join("|");
  if (text.includes("too many requests")) return "gateway_rate_limited";
  const rateLimitFailure = new RegExp(
    `(?:${failure})[\\s\\S]{0,200}(?:${rateLimit})|(?:${rateLimit})[\\s\\S]{0,200}(?:${failure})`
  );
  if (rateLimitFailure.test(text)) return "gateway_rate_limited";

  // Require an explicit AI Gateway / API Error prefix or machine-readable
  // spend code on the same line, never loose "403" + "budget" anywhere in the
  // 4 KB terminal scrollback (Seat 23 built a budget-vs-actual app and got a
  // false allowance banner, EQL 27 Oct 2026).
  const explicitAllowance =
    /\bservice_spend_limit_reached\b|(?:api\s+error|error)\s*[: ]\s*(?:40[39]|429)[^\r\n]{0,160}\b(?:ai\s+gateway\s+budget|budget\s+policy\s+limit|spend[ _-]limit|allowance\s+exhausted|quota[ _-]exhausted|out[ _-]of[ _-]credits)\b/;
  if (explicitAllowance.test(text)) return "gateway_allowance_exhausted";
  return null;
}
