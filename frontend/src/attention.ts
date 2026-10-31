// Wrap reminders reach an attendee who is working in another tab (R-012).
//
// A broadcast arrives as a WT toast, and a toast is only seen on the WT page.
// An attendee on the Omni tab (or any other tab) sees nothing, so while this
// page is hidden the title flashes until they return, and a browser notification
// goes up when the attendee has already granted permission. Nothing requests
// permission here: a request needs a user gesture this page does not have at
// the moment a reminder lands.
//
// There is one flash for the page, not one per message. The original title is
// saved once, when the flash starts; a later message updates the label of the
// running flash. The interval and the visibility listener are shared, so a
// second reminder cannot save the flashed title as the original.

export interface AttentionSurface {
  /** True while the page is in a background tab or minimised. */
  readonly hidden: boolean;
  readonly title: string;
  setTitle(value: string): void;
  /** Subscribe to the tab becoming visible; returns the unsubscribe. */
  onVisible(listener: () => void): () => void;
  /** A browser notification, when the attendee has granted permission. */
  notify?(title: string, body: string): void;
}

export interface AttentionOptions {
  intervalMs?: number;
  maxFlashes?: number;
  schedule?: (fn: () => void, ms: number) => unknown;
  cancel?: (handle: unknown) => void;
}

interface Flash {
  surface: AttentionSurface;
  original: string;
  alternate: string;
  showingAlternate: boolean;
  flashes: number;
  maxFlashes: number;
  handle: unknown;
  cancel: (handle: unknown) => void;
  unsubscribe: () => void;
}

/** The one running flash for this page, or null. */
let active: Flash | null = null;

/** The title shown during a flash. Short, so it survives a truncated tab strip. */
export function flashTitle(message: string): string {
  const label = message.trim().split(/\s+/).slice(0, 4).join(" ");
  return `(!) ${label}`;
}

/** Stop the running flash, if any, and put the original title back. */
export function stopFlash(): void {
  const flash = active;
  if (!flash) return;
  active = null;
  flash.cancel(flash.handle);
  flash.unsubscribe();
  flash.surface.setTitle(flash.original);
}

/**
 * Tell an attendee who is away that a message arrived. Every message notifies;
 * only the first starts the flash. Returns a function that stops the flash.
 * Nothing happens while the page is visible: the toast already covers that case.
 */
export function announceWhileAway(
  surface: AttentionSurface,
  message: string,
  options: AttentionOptions = {},
): () => void {
  if (!surface.hidden || !message.trim()) return stopFlash;
  surface.notify?.("Workshop update", message.trim());

  if (active) {
    // Already flashing: the newest message becomes the label, and the count
    // starts again so the flash stays up for this reminder too.
    active.alternate = flashTitle(message);
    active.flashes = 0;
    return stopFlash;
  }

  const schedule = options.schedule ?? ((fn: () => void, ms: number) => setInterval(fn, ms));
  const cancel = options.cancel ?? ((handle: unknown) => clearInterval(handle as ReturnType<typeof setInterval>));
  const flash: Flash = {
    surface,
    original: surface.title,
    alternate: flashTitle(message),
    showingAlternate: false,
    flashes: 0,
    maxFlashes: options.maxFlashes ?? 20,
    handle: undefined,
    cancel,
    unsubscribe: () => undefined,
  };
  active = flash;
  flash.unsubscribe = surface.onVisible(stopFlash);
  flash.handle = schedule(() => tick(flash), options.intervalMs ?? 1200);
  return stopFlash;
}

function tick(flash: Flash): void {
  if (active !== flash) return;
  flash.flashes += 1;
  if (flash.flashes > flash.maxFlashes) {
    // A forgotten tab is not flashed forever.
    stopFlash();
    return;
  }
  flash.showingAlternate = !flash.showingAlternate;
  flash.surface.setTitle(flash.showingAlternate ? flash.alternate : flash.original);
}

type BrowserDocument = Pick<Document, "title" | "hidden" | "addEventListener" | "removeEventListener">;

/** A notification only when the attendee has already granted permission. */
function notifyIfGranted(title: string, body: string): void {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  try {
    new Notification(title, { body, tag: "workshop-update" });
  } catch {
    /* a notification the browser refuses costs nothing: the title still flashes */
  }
}

/** The real browser surface: document title, visibility and notifications. */
export function browserAttentionSurface(
  doc: BrowserDocument = document,
  notify: (title: string, body: string) => void = notifyIfGranted,
): AttentionSurface {
  return {
    get hidden() { return doc.hidden; },
    get title() { return doc.title; },
    setTitle(value: string) { doc.title = value; },
    onVisible(listener: () => void) {
      const onChange = () => { if (!doc.hidden) listener(); };
      doc.addEventListener("visibilitychange", onChange);
      return () => doc.removeEventListener("visibilitychange", onChange);
    },
    notify,
  };
}
