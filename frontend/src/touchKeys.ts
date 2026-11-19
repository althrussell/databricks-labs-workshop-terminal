/** The keys a phone's soft keyboard lacks, as the bytes a terminal sends.
 *
 * Agent TUIs ask questions with menus: arrows move, Enter picks, Esc backs out,
 * Shift+Tab cycles modes and Ctrl+C interrupts. Without a hardware keyboard an
 * attendee has no way to press them, so the touch key bar sends these bytes.
 */

export type TouchKey =
  | "esc"
  | "tab"
  | "shiftTab"
  | "up"
  | "down"
  | "left"
  | "right"
  | "enter"
  | "ctrlC";

export const TOUCH_KEYS: readonly { key: TouchKey; label: string; name: string }[] = [
  { key: "esc", label: "Esc", name: "Escape" },
  { key: "tab", label: "Tab", name: "Tab" },
  { key: "shiftTab", label: "⇧Tab", name: "Shift+Tab" },
  { key: "up", label: "↑", name: "Up arrow" },
  { key: "down", label: "↓", name: "Down arrow" },
  { key: "left", label: "←", name: "Left arrow" },
  { key: "right", label: "→", name: "Right arrow" },
  { key: "enter", label: "⏎", name: "Enter" },
  { key: "ctrlC", label: "^C", name: "Ctrl+C (interrupt)" },
];

const ARROWS: Record<"up" | "down" | "left" | "right", string> = {
  up: "A",
  down: "B",
  right: "C",
  left: "D",
};

/** Bytes for `key`. `appCursor` is the terminal's DECCKM mode: TUIs that turn it
 * on expect `ESC O A` for an arrow, as a hardware keyboard would send. */
export function keySequence(key: TouchKey, appCursor: boolean): string {
  switch (key) {
    case "esc":
      return "\x1b";
    case "tab":
      return "\t";
    case "shiftTab":
      return "\x1b[Z";
    case "enter":
      return "\r";
    case "ctrlC":
      return "\x03";
    default:
      return (appCursor ? "\x1bO" : "\x1b[") + ARROWS[key];
  }
}
