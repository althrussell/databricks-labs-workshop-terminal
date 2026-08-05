/** Toast stack rules, kept out of the component so they can be tested.
 *
 * The whole point of moving operator replies off the banner was that the banner
 * had one slot: a second reply overwrote the first and nobody knew. A queue that
 * evicts to stay short reintroduces exactly that, so the eviction rule is worth
 * more than an eyeball.
 */

import type { NotificationDurability } from "./events";

export type ToastLevel = "info" | "success" | "warning" | "error";

export interface Toast {
  id: string;
  level: ToastLevel;
  durability: NotificationDurability;
  title: string;
  body: string;
  /** How long a transient toast lives. Absent means the default. */
  ttlMs?: number;
  /** Set when Control Tower asked for a read receipt. */
  ackMessageId?: string;
  /** Operator replies deep-link into the conversation. */
  openHelp?: boolean;
}

/** How many *transient* toasts may stack before the oldest is dropped. */
export const MAX_TRANSIENT = 3;
/** Used when the sender expressed no preference. */
export const TRANSIENT_MS = 8000;
/** Ceiling on a transient toast, so "transient" keeps meaning something.
 *
 * Well above Control Tower's 300s broadcast default, which is the number this
 * has to respect: the point of reading ttl_s at all is that a five-minute
 * pacing notice should last five minutes. The ceiling only exists because the
 * wire contract permits up to 86400, and a toast that promises to go away
 * should not sit there for a day.
 */
export const MAX_TRANSIENT_MS = 30 * 60_000;

/** Add one toast, dropping only surplus pacing chatter.
 *
 * Sticky and critical toasts are never evicted to make room. They are dismissed
 * by the attendee or not at all, so dropping one to honour a stack limit would
 * throw away an answer nobody has read — and the operator would still see it as
 * delivered. Overflow is a layout problem (the host scrolls), not a reason to
 * discard a message.
 */
export function pushToast(stack: Toast[], toast: Toast): Toast[] {
  if (stack.some((t) => t.id === toast.id)) return stack;
  const next = [...stack, toast];
  const transient = next.filter((t) => t.durability === "transient");
  if (transient.length <= MAX_TRANSIENT) return next;
  const surplus = new Set(
    transient.slice(0, transient.length - MAX_TRANSIENT).map((t) => t.id),
  );
  return next.filter((t) => !surplus.has(t.id));
}

/** Seconds from the server, clamped, or undefined to mean "use the default". */
export function transientTtlMs(ttlSeconds: number | undefined): number | undefined {
  if (typeof ttlSeconds !== "number" || ttlSeconds <= 0) return undefined;
  return Math.min(ttlSeconds * 1000, MAX_TRANSIENT_MS);
}
