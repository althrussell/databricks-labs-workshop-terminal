import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, Info, MessageSquare, X } from "lucide-react";
import { api } from "../api";
import { onAppEvent } from "../events";
import {
  Toast,
  ToastLevel,
  TRANSIENT_MS,
  pushToast,
  transientTtlMs,
} from "../toasts";

/** Stacking toast host for messages addressed to this attendee.
 *
 * Replaces the banner hijack that used to render operator replies. The banner
 * has one slot and one timer, so a second reply overwrote the first and a direct
 * answer was dismissed on the same clock as a room-wide announcement. Toasts
 * stack, and durability decides whether one may dismiss itself:
 *
 * - `transient` auto-dismisses. Pacing chatter; missing one costs nothing.
 * - `sticky` has no timer. A direct answer to a question this attendee asked.
 * - `critical` has no timer and is visually loud. A lock or a suspension.
 *
 * Nothing here invents durable storage: a missed toast is still recoverable from
 * the help panel, whose unread counter this feeds.
 */

const ICONS = {
  info: Info,
  success: CheckCircle2,
  warning: AlertTriangle,
  error: AlertTriangle,
} as const;

function level(value: string | undefined): ToastLevel {
  return value && value in ICONS ? (value as ToastLevel) : "info";
}

export default function ToastHost({
  onOpenHelp,
}: {
  /** Opens the help conversation, so a reply is one click from its thread. */
  onOpenHelp?: () => void;
}) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  // Acks are fire-and-forget and must happen exactly once per message, even
  // though React may render a toast twice in development strict mode.
  const acked = useRef<Set<string>>(new Set());

  const push = useCallback((toast: Toast) => {
    setToasts((prev) => pushToast(prev, toast));
  }, []);

  const dismiss = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  useEffect(
    () =>
      onAppEvent((event) => {
        if (event.t === "help_message") {
          // The attendee's own messages need no notification, and neither does
          // anything Control Tower routed away from the toast surface.
          if (event.sender_role !== "operator") return;
          if (event.surface && event.surface !== "toast") return;
          push({
            id: `help:${event.message_id}`,
            level: "info",
            durability: event.durability ?? "sticky",
            title: event.sender ? `${event.sender} replied` : "Operator replied",
            body: event.body,
            ackMessageId: event.request_ack === false ? undefined : event.message_id,
            openHelp: true,
          });
          return;
        }
        if (event.t === "broadcast") {
          if (event.clear) return;
          // Banners are the other component's job.
          if ((event.surface ?? "toast") !== "toast") return;
          if (!event.message.trim()) return;
          push({
            id: `broadcast:${event.message}:${Date.now()}`,
            level: level(event.level),
            durability: event.durability ?? "transient",
            title: "Workshop update",
            body: event.message,
            // The operator chose how long this should stand. A pacing notice
            // sent with five minutes on it should not vanish in eight seconds
            // because the default happened to be shorter.
            ttlMs: transientTtlMs(event.ttl_s),
          });
        }
      }),
    [push]
  );

  return (
    <div className="toast-host" role="region" aria-label="Notifications">
      {toasts.map((toast) => (
        <ToastCard
          key={toast.id}
          toast={toast}
          acked={acked.current}
          // Passed by reference rather than wrapped in a closure: an inline
          // arrow would be a new value on every render of this component, and
          // the card's auto-dismiss effect depends on it. Websocket traffic
          // re-renders this often enough that the timer would be cleared and
          // restarted forever, so transient toasts would never leave.
          onDismiss={dismiss}
          onOpenHelp={onOpenHelp}
        />
      ))}
    </div>
  );
}

function ToastCard({
  toast,
  acked,
  onDismiss,
  onOpenHelp,
}: {
  toast: Toast;
  acked: Set<string>;
  onDismiss: (id: string) => void;
  onOpenHelp?: () => void;
}) {
  const Icon = toast.openHelp ? MessageSquare : ICONS[toast.level];
  const close = useCallback(() => onDismiss(toast.id), [onDismiss, toast.id]);

  // Rendering is the moment the attendee could have seen it, so that is when the
  // receipt is sent. Failure is silent: an operator seeing "delivered, not seen"
  // is a correct pessimistic reading, and a toast is not the place to report it.
  useEffect(() => {
    const id = toast.ackMessageId;
    if (!id || acked.has(id)) return;
    acked.add(id);
    void api.ackHelpMessage(id).catch(() => undefined);
  }, [toast.ackMessageId, acked]);

  useEffect(() => {
    if (toast.durability !== "transient") return;
    const timer = setTimeout(close, toast.ttlMs ?? TRANSIENT_MS);
    return () => clearTimeout(timer);
  }, [toast.durability, toast.ttlMs, close]);

  return (
    <div
      className={`toast toast-${toast.level} toast-${toast.durability}`}
      role={toast.durability === "critical" ? "alert" : "status"}
    >
      <Icon size={16} className="toast-icon" />
      <div className="toast-content">
        <div className="toast-title">{toast.title}</div>
        <div className="toast-body">{toast.body}</div>
        {toast.openHelp && onOpenHelp ? (
          <button type="button" className="toast-action" onClick={onOpenHelp}>
            Open conversation
          </button>
        ) : null}
      </div>
      <button
        type="button"
        className="icon-btn toast-dismiss"
        onClick={close}
        aria-label="Dismiss notification"
      >
        <X size={14} />
      </button>
    </div>
  );
}
