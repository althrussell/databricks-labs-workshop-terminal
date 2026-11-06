/** Keyboard conventions attendees bring from their desktop terminals.
 *
 * xterm.js turns Ctrl+<letter> into a control byte and cancels the browser's
 * default action. That is right for Ctrl+D or Ctrl+R and wrong for the two
 * chords every Windows and Linux user reaches for first:
 *
 * - Ctrl+C on a selection would send SIGINT and cancel the agent's turn (twice
 *   in a row exits the CLI). With a selection it copies; without one it still
 *   interrupts, as every terminal does.
 * - Ctrl+V would send ^V, which Claude Code and Codex read as "paste an image
 *   from the clipboard" — the container's clipboard, not the laptop's, so
 *   nothing arrives. It goes back to the browser instead, whose native paste
 *   event xterm already turns into bracketed input.
 *
 * Ctrl+Shift+C and Ctrl+Shift+V are the Linux terminal chords. xterm sends no
 * byte for either, but the browser's own Ctrl+Shift+C opens Chrome's element
 * inspector rather than copying, so it is claimed as a copy. Ctrl+Shift+V is
 * already the browser's plain-text paste and is left to it.
 *
 * macOS is untouched: Cmd+C and Cmd+V copy and paste natively, and Ctrl+V there
 * stays the control byte a Mac terminal sends.
 */

export interface KeyLike {
  type: string;
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
}

/**
 * - `copy`: the caller copies the selection and cancels the browser default.
 * - `browser`: xterm must not see the key; the browser's default (paste) runs.
 * - `terminal`: xterm handles the key as usual.
 */
export type KeyDecision = "copy" | "browser" | "terminal";

export function decideTerminalKey(event: KeyLike, hasSelection: boolean, isMac = false): KeyDecision {
  if (event.type !== "keydown") return "terminal";
  if (!event.ctrlKey || event.metaKey || event.altKey) return "terminal";
  const key = event.key.toLowerCase();
  if (key === "c") {
    // Ctrl+Shift+C always copies, so it can never open the inspector.
    if (event.shiftKey) return "copy";
    return hasSelection ? "copy" : "terminal";
  }
  if (key === "v") {
    if (event.shiftKey) return "browser";
    return isMac ? "terminal" : "browser";
  }
  return "terminal";
}
