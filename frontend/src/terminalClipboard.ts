/** Clipboard plumbing for the terminal's copy and paste.
 *
 * Two browser paths reach the clipboard, and they fail differently:
 *
 * - Native clipboard events (Cmd+C/V, Ctrl+V, Ctrl+Shift+V, the right-click
 *   menu) need no permission, and xterm already listens for them.
 * - The async Clipboard API, which Ctrl+C and the Copy/Paste buttons need,
 *   requires a secure context and a focused document; reading also needs the
 *   attendee's permission, and an iframe needs its embedder's permission policy.
 *
 * So a refused copy falls back to the legacy `execCommand("copy")` path, and a
 * refused read becomes a paste box the attendee pastes into natively. Nothing
 * here fails silently: every outcome maps to a line the attendee can act on.
 * The functions take their browser surface as an argument so they can be
 * tested without one.
 */

export interface ClipboardEnv {
  clipboard?: {
    writeText?: (text: string) => Promise<void>;
    readText?: () => Promise<string>;
  };
  /** `window.isSecureContext`: the async API is absent or refused without it. */
  secure: boolean;
  /** Synchronous `execCommand("copy")` fallback; true when the browser copied. */
  legacyCopy: (text: string) => boolean;
}

/** `empty`: nothing was selected. `blocked`: every clipboard path was refused. */
export type CopyOutcome = "copied" | "empty" | "blocked";

/** `blocked` covers denied and unavailable alike: both get the paste box. */
export type PasteRead =
  | { kind: "text"; text: string }
  | { kind: "empty" }
  | { kind: "blocked" };

export type PasteOutcome = "pasted" | "empty" | "offline" | "image";

export type ClipboardNoticeLevel = "success" | "info" | "warning";

export interface ClipboardNotice {
  level: ClipboardNoticeLevel;
  text: string;
}

export async function copyText(text: string, env: ClipboardEnv): Promise<CopyOutcome> {
  if (!text) return "empty";
  if (env.secure && env.clipboard?.writeText) {
    try {
      await env.clipboard.writeText(text);
      return "copied";
    } catch {
      // Denied, document not focused, or an iframe without clipboard-write.
      // The click or key press that started this is still a user gesture.
    }
  }
  return env.legacyCopy(text) ? "copied" : "blocked";
}

export async function readClipboardText(env: ClipboardEnv): Promise<PasteRead> {
  if (!env.secure || !env.clipboard?.readText) return { kind: "blocked" };
  try {
    const text = await env.clipboard.readText();
    // An image-only clipboard reads as "": there is nothing to type.
    return text ? { kind: "text", text } : { kind: "empty" };
  } catch {
    return { kind: "blocked" };
  }
}

/** A native paste that carries a screenshot but no text types nothing at all. */
export function pasteIsImageOnly(data: Pick<DataTransfer, "getData" | "types"> | null): boolean {
  if (!data || data.getData("text/plain")) return false;
  return Array.from(data.types).includes("Files");
}

export function copyNotice(outcome: CopyOutcome, isMac: boolean): ClipboardNotice {
  if (outcome === "copied") return { level: "success", text: "Copied to your clipboard." };
  if (outcome === "empty") {
    const force = isMac ? "hold ⌥ Option" : "hold Shift";
    return {
      level: "info",
      text: `Select text in the terminal first. If dragging doesn't highlight it, ${force} while you drag.`,
    };
  }
  const manual = isMac ? "press ⌘C, or right-click it and choose Copy" : "right-click it and choose Copy";
  return {
    level: "warning",
    text: `The browser blocked clipboard access. The text is still selected: ${manual}.`,
  };
}

export function pasteNotice(outcome: PasteOutcome): ClipboardNotice {
  switch (outcome) {
    case "pasted":
      return { level: "success", text: "Pasted. Press Enter to send it." };
    case "empty":
      return { level: "info", text: "Your clipboard has no text to paste. For a screenshot, use Attach image." };
    case "image":
      return { level: "info", text: "Images can't be pasted into the terminal. Use Attach image instead." };
    case "offline":
      return { level: "warning", text: "The terminal isn't connected right now. Try again in a moment." };
  }
}

interface PlatformLike {
  platform?: string;
  userAgent?: string;
  userAgentData?: { platform?: string };
}

export function isMacPlatform(nav: PlatformLike | undefined): boolean {
  if (!nav) return false;
  const platform = nav.userAgentData?.platform || nav.platform || nav.userAgent || "";
  return /mac|iphone|ipad|ipod/i.test(platform);
}

/** Read once: the attendee's modifier keys don't change mid-session. */
export const IS_MAC = isMacPlatform(typeof navigator === "undefined" ? undefined : navigator);

/** The copy and paste chords to name in titles and hints. */
export const COPY_CHORD = IS_MAC ? "⌘C" : "Ctrl+C";
export const PASTE_CHORD = IS_MAC ? "⌘V" : "Ctrl+V";

/** The pre-Clipboard-API copy: select a scratch textarea and `execCommand`.
 *
 * Works without the async API's permission and inside iframes, as long as it
 * runs within the user gesture. The capture listener writes the exact text so
 * nothing else on the page (xterm's own copy handler) can substitute its own.
 */
export function legacyCopy(text: string): boolean {
  if (typeof document === "undefined" || typeof document.execCommand !== "function") return false;
  const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const scratch = document.createElement("textarea");
  scratch.value = text;
  scratch.setAttribute("readonly", "");
  scratch.setAttribute("aria-hidden", "true");
  scratch.tabIndex = -1;
  scratch.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;pointer-events:none;";
  const onCopy = (event: ClipboardEvent) => {
    if (!event.clipboardData) return;
    event.clipboardData.setData("text/plain", text);
    event.preventDefault();
    event.stopImmediatePropagation();
  };
  document.body.appendChild(scratch);
  document.addEventListener("copy", onCopy, true);
  try {
    scratch.focus({ preventScroll: true });
    scratch.select();
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    document.removeEventListener("copy", onCopy, true);
    scratch.remove();
    previous?.focus({ preventScroll: true });
  }
}

export function browserClipboardEnv(): ClipboardEnv {
  return {
    clipboard: typeof navigator === "undefined" ? undefined : navigator.clipboard,
    secure: typeof window !== "undefined" && window.isSecureContext,
    legacyCopy,
  };
}
