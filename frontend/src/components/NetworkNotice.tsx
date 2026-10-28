import { useEffect, useState } from "react";
import { RefreshCw, Wifi, WifiOff, X } from "lucide-react";
import {
  formatNetworkSummary,
  isOutageShaped,
  type NetworkCheckResult,
  type NetworkStatusKind,
} from "../networkCheck";

export const NETWORK_BLOCKED_COPY =
  "This network is holding back live updates. New Claude chats in Omnigent won't start here. Switch to guest Wi-Fi or a phone hotspot, or use Claude in the terminal.";

export const NETWORK_OUTAGE_COPY =
  "Connection lost. Checking again automatically; this clears once you're back online.";

export function NetworkNotice({ result }: { result: NetworkCheckResult | null }) {
  const [dismissedCheckedAt, setDismissedCheckedAt] = useState<string | null>(null);
  const verdict = result?.verdict ?? null;
  const checkedAt = result?.checked_at ?? null;

  useEffect(() => {
    if (verdict && verdict !== "blocked") {
      setDismissedCheckedAt(null);
    }
  }, [verdict]);

  if (verdict !== "blocked" || (checkedAt !== null && dismissedCheckedAt === checkedAt)) {
    return null;
  }

  return (
    <div className="banner banner-warning network-notice" role="status" aria-live="polite">
      <WifiOff size={16} aria-hidden="true" />
      <span>{isOutageShaped(result) ? NETWORK_OUTAGE_COPY : NETWORK_BLOCKED_COPY}</span>
      <button
        type="button"
        className="icon-btn"
        aria-label="Dismiss network notice"
        onClick={() => setDismissedCheckedAt(checkedAt ?? "dismissed")}
      >
        <X size={14} />
      </button>
    </div>
  );
}

export function NetworkStatusControl({
  status,
  result,
  onRunAgain,
}: {
  status: NetworkStatusKind;
  result: NetworkCheckResult | null;
  onRunAgain: () => void;
}) {
  const [open, setOpen] = useState(false);
  const summary = formatNetworkSummary(result);
  const dotClass =
    status === "ok"
      ? "dot-online"
      : status === "Blocked".toLowerCase()
        ? "dot-error"
        : "dot-warn";
  const label =
    status === "checking"
      ? "Checking network…"
      : status === "ok"
        ? "Network: OK"
        : status === "slow"
          ? "Network: slow"
          : isOutageShaped(result)
            ? "Network: connection lost"
            : "Network: live updates held back";

  return (
    <div className="network-status-wrap" style={{ position: "relative", display: "inline-flex" }}>
      <button
        type="button"
        className="operator-toggle network-status-btn"
        aria-label={label}
        aria-expanded={open}
        title={label}
        onClick={() => setOpen((v) => !v)}
      >
        {status === "blocked" ? (
          <WifiOff size={15} aria-hidden="true" />
        ) : (
          <Wifi size={15} aria-hidden="true" />
        )}
        <span className={`dot ${dotClass}`} />
      </button>
      {open && (
        <div
          className="network-popover"
          role="dialog"
          aria-label="Network check details"
          style={{
            position: "absolute",
            top: "calc(100% + 6px)",
            right: 0,
            zIndex: 50,
            minWidth: "240px",
            padding: "10px 12px",
            borderRadius: "8px",
            background: "var(--surface-elevated, #182026)",
            border: "1px solid var(--border-subtle, rgba(255,255,255,0.12))",
            boxShadow: "0 8px 24px rgba(0,0,0,0.35)",
            fontSize: "12px",
            lineHeight: 1.5,
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: "4px" }}>{summary.liveUpdates}</div>
          <div>{summary.terminalConnection}</div>
          <div style={{ opacity: 0.75, marginTop: "4px" }}>{summary.checkedTime}</div>
          <div style={{ marginTop: "8px", display: "flex", justifyContent: "flex-end" }}>
            <button
              type="button"
              className="btn btn-ghost"
              disabled={status === "checking"}
              onClick={() => onRunAgain()}
              style={{ fontSize: "12px", padding: "3px 8px" }}
            >
              <RefreshCw size={12} className={status === "checking" ? "spin" : ""} /> Run again
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
