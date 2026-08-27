import { useEffect, useRef } from "react";
import { ArrowRight, Loader2 } from "lucide-react";

interface Props {
  currentLabel: string;
  requestedLabel: string;
  busy: boolean;
  error: string;
  onCancel: () => void;
  onConfirm: () => void;
}

export default function AgentSwitchDialog({
  currentLabel,
  requestedLabel,
  busy,
  error,
  onCancel,
  onConfirm,
}: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    const returnFocus = document.activeElement as HTMLElement | null;
    if (dialog && !dialog.open) dialog.showModal();
    return () => {
      if (dialog?.open) dialog.close();
      returnFocus?.focus();
    };
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="modal agent-switch-dialog"
      aria-labelledby="agent-switch-title"
      aria-describedby="agent-switch-description"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onCancel();
      }}
      onKeyDown={(event) => {
        if (event.key !== "Escape" || busy) return;
        event.preventDefault();
        event.stopPropagation();
        onCancel();
      }}
    >
      <h2 id="agent-switch-title">Close {currentLabel}?</h2>
      <p id="agent-switch-description">
        Only one agent can run at a time. Close {currentLabel} before opening{" "}
        {requestedLabel}.
      </p>
      {error && (
        <p className="agent-switch-error" role="alert">
          {error}
        </p>
      )}
      <div className="modal-actions">
        <button className="phase-btn" disabled={busy} onClick={onCancel} autoFocus>
          Cancel
        </button>
        <button className="primary-btn" disabled={busy} onClick={onConfirm}>
          {busy ? <Loader2 size={14} className="spin" /> : <ArrowRight size={14} />}
          {busy ? "Switching…" : `Close and open ${requestedLabel}`}
        </button>
      </div>
    </dialog>
  );
}
