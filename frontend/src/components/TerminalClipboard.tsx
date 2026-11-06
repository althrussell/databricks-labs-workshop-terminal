import { useEffect, useRef, useState, type RefObject } from "react";
import { ClipboardPaste, Copy, X } from "lucide-react";
import { trapDialogTab } from "../dialog";
import {
  browserClipboardEnv,
  COPY_CHORD,
  PASTE_CHORD,
  pasteNotice,
  readClipboardText,
  type ClipboardNotice,
} from "../terminalClipboard";
import type { TerminalHandle } from "./TerminalView";

interface Props {
  terminal: RefObject<TerminalHandle | null>;
  notice: ClipboardNotice | null;
  onNotice: (notice: ClipboardNotice | null) => void;
  /** False once the session has ended or the workshop stopped taking input. */
  canPaste: boolean;
}

/** Long enough to read the hint and act on it; a success needs only a glance. */
const NOTICE_MS: Record<ClipboardNotice["level"], number> = {
  success: 3000,
  info: 8000,
  warning: 12000,
};

/** Visible Copy and Paste for the terminal, for anyone whose shortcut didn't work.
 *
 * Copy works on an ended session too: its output is still worth keeping. Paste
 * asks the browser for the clipboard; when the browser says no, the attendee
 * gets a box to paste into with their own shortcut, which needs no permission.
 */
export default function TerminalClipboard({ terminal, notice, onNotice, canPaste }: Props) {
  const [reading, setReading] = useState(false);
  const [boxOpen, setBoxOpen] = useState(false);
  const pasteButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!canPaste) setBoxOpen(false);
  }, [canPaste]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => onNotice(null), NOTICE_MS[notice.level]);
    return () => window.clearTimeout(timer);
  }, [notice, onNotice]);

  function insert(text: string) {
    const sent = terminal.current?.paste(text) ?? false;
    onNotice(pasteNotice(sent ? "pasted" : "offline"));
    return sent;
  }

  async function pasteFromClipboard() {
    if (reading) return;
    setReading(true);
    try {
      const read = await readClipboardText(browserClipboardEnv());
      if (read.kind === "blocked") {
        setBoxOpen(true);
        return;
      }
      if (read.kind === "text") insert(read.text);
      else onNotice(pasteNotice("empty"));
      terminal.current?.focus();
    } finally {
      setReading(false);
    }
  }

  return (
    <>
      <button
        type="button"
        className="btn btn-ghost"
        title={`Copy the highlighted terminal text (${COPY_CHORD} also works)`}
        onClick={() => {
          terminal.current?.copySelection();
          terminal.current?.focus();
        }}
      >
        <Copy size={14} aria-hidden="true" /> Copy
      </button>
      {canPaste && (
        <button
          ref={pasteButtonRef}
          type="button"
          className="btn btn-ghost"
          disabled={reading}
          title={`Paste your clipboard into the prompt (${PASTE_CHORD} also works)`}
          onClick={() => void pasteFromClipboard()}
        >
          <ClipboardPaste size={14} aria-hidden="true" /> {reading ? "Pasting…" : "Paste"}
        </button>
      )}
      {notice && (
        <span className={`clipboard-notice clipboard-notice-${notice.level}`} role="status" aria-live="polite">
          <span>{notice.text}</span>
          <button type="button" className="icon-btn" aria-label="Dismiss" onClick={() => onNotice(null)}>
            <X size={12} aria-hidden="true" />
          </button>
        </span>
      )}
      {boxOpen && (
        <PasteBox
          onInsert={insert}
          onClose={(inserted) => {
            setBoxOpen(false);
            if (inserted) terminal.current?.focus();
            else pasteButtonRef.current?.focus();
          }}
        />
      )}
    </>
  );
}

function PasteBox({
  onInsert,
  onClose,
}: {
  /** True once the text reached the terminal. */
  onInsert: (text: string) => boolean;
  onClose: (inserted: boolean) => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
    return () => {
      if (dialog?.open) dialog.close();
    };
  }, []);

  // Close before handing focus back: closing a modal restores focus to
  // whatever opened it, which would otherwise undo the parent's choice.
  function finish(inserted: boolean) {
    dialogRef.current?.close();
    onClose(inserted);
  }

  function submit() {
    if (!text) return;
    if (onInsert(text)) finish(true);
    else setError(pasteNotice("offline").text);
  }

  return (
    <dialog
      ref={dialogRef}
      className="modal paste-box-dialog"
      aria-labelledby="paste-box-title"
      aria-describedby="paste-box-description"
      onCancel={(event) => {
        event.preventDefault();
        finish(false);
      }}
      onKeyDown={trapDialogTab}
    >
      <h2 id="paste-box-title">
        <ClipboardPaste size={18} aria-hidden="true" /> Paste into the terminal
      </h2>
      <p id="paste-box-description">
        Your browser didn't let this page read the clipboard. Paste here with {PASTE_CHORD}, then
        choose Insert. Nothing is sent until you press Enter in the terminal.
      </p>
      {error && (
        <p className="agent-switch-error" role="alert">
          {error}
        </p>
      )}
      <textarea
        className="paste-box-input"
        aria-label="Text to paste into the terminal"
        autoFocus
        rows={8}
        spellCheck={false}
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
            event.preventDefault();
            submit();
          }
        }}
      />
      <div className="modal-actions">
        <button type="button" className="phase-btn" onClick={() => finish(false)}>
          Cancel
        </button>
        <button type="button" className="primary-btn" disabled={!text} onClick={submit}>
          Insert
        </button>
      </div>
    </dialog>
  );
}
