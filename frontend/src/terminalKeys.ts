/** Keyboard conventions attendees bring from their desktop terminals.
 *
 * xterm.js forwards every key to the PTY. On Windows and Linux the natural
 * "select a URL, press Ctrl+C" therefore sends SIGINT and cancels the agent's
 * turn (twice in a row exits the CLI). This module decides, from the key event
 * and the selection state alone, whether a key should be handled by the browser
 * (copy) instead of reaching the terminal. macOS Cmd+C already copies natively.
 */

export interface KeyLike {
  type: string;
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
}

export type KeyDecision = "copy" | "browser" | "terminal";

export function decideTerminalKey(event: KeyLike, hasSelection: boolean): KeyDecision {
  if (event.type !== "keydown") return "terminal";
  const key = event.key.toLowerCase();
  const ctrlOnly = event.ctrlKey && !event.metaKey && !event.altKey;
  if (ctrlOnly && !event.shiftKey && key === "c" && hasSelection) {
    // Copy the selection; the agent never sees a stray interrupt.
    return "copy";
  }
  if (ctrlOnly && event.shiftKey && (key === "c" || key === "v")) {
    // Ctrl+Shift+C / Ctrl+Shift+V are the Linux terminal copy/paste chords.
    // Let the browser handle them rather than sending control bytes.
    return "browser";
  }
  return "terminal";
}
