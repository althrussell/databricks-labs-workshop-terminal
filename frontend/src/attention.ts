// Wrap reminders reach an attendee who is working in another tab (R-012).
//
// A broadcast arrives as a WT toast, and a toast is only seen on the WT page.
// An attendee on the Omni tab (or any other tab) sees nothing, so while this
// page is hidden the title flashes until they return, and a browser notification
// goes up when the attendee has already granted permission. Nothing requests
// permission here: a request needs a user gesture this page does not have at
// the moment a reminder lands.

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

/** The title shown during a flash. Short, so it survives a truncated tab strip. */
export function flashTitle(message: string): string {
  const label = message.trim().split(/\s+/).slice(0, 4).join(" ");
  return `(!) ${label}`;
}

/**
 * Tell an attendee who is away that a message arrived. Returns a function that
 * stops the flashing early. Nothing happens while the page is visible: the toast
 * already covers that case.
 */
export function announceWhileAway(
  surface: AttentionSurface,
  message: string,
  options: AttentionOptions = {},
): () => void {
  if (!surface.hidden || !message.trim()) return () => undefined;
  const intervalMs = options.intervalMs ?? 1200;
  const maxFlashes = options.maxFlashes ?? 20;
  const schedule = options.schedule ?? ((fn, ms) => setInterval(fn, ms));
  const cancel = options.cancel ?? ((handle) => clearInterval(handle as ReturnType<typeof setInterval>));
  const original = surface.title;
  const alternate = flashTitle(message);
  let flashes = 0;
  let showingAlternate = false;
  let stopped = false;

  surface.notify?.("Workshop update", message.trim());

  const finish = () => {
    if (stopped) return;
    stopped = true;
    cancel(handle);
    unsubscribe();
    surface.setTitle(original);
  };
  const tick = () => {
    if (stopped) return;
    flashes += 1;
    if (flashes > maxFlashes) return finish();
    showingAlternate = !showingAlternate;
    surface.setTitle(showingAlternate ? alternate : original);
  };
  const unsubscribe = surface.onVisible(finish);
  const handle = schedule(tick, intervalMs);
  return finish;
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
