import { useEffect, useMemo, useRef, useState } from "react";
import { Check, Copy, Smartphone } from "lucide-react";
import { trapDialogTab } from "../dialog";
import { phoneUrl, qrPath } from "../qr";
import { browserClipboardEnv, copyText } from "../terminalClipboard";

/** Quiet zone around the code, in modules; scanners need four. */
const MARGIN = 4;

/** Header control: a QR code that opens this workshop on a phone. */
export default function OpenOnPhone() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        className="operator-toggle"
        onClick={() => setOpen(true)}
        aria-label="Open on phone"
        title="Open on phone — scan to continue on your phone"
      >
        <Smartphone size={16} aria-hidden="true" />
        <span className="nav-label">Open on phone</span>
      </button>
      {open && <PhoneDialog onClose={() => setOpen(false)} />}
    </>
  );
}

function PhoneDialog({ onClose }: { onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const url = phoneUrl(window.location);
  const { size, d } = useMemo(() => qrPath(url), [url]);
  const [copied, setCopied] = useState(false);
  const box = size + MARGIN * 2;

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
    return () => {
      if (dialog?.open) dialog.close();
    };
  }, []);

  function finish() {
    dialogRef.current?.close();
    onClose();
  }

  return (
    <dialog
      ref={dialogRef}
      className="modal phone-dialog"
      aria-labelledby="phone-dialog-title"
      onCancel={(event) => {
        event.preventDefault();
        finish();
      }}
      onClick={(event) => {
        // A click on the backdrop lands on the dialog element itself.
        if (event.target === event.currentTarget) finish();
      }}
      onKeyDown={trapDialogTab}
    >
      <div className="phone-dialog-body">
        <h2 id="phone-dialog-title">
          <Smartphone size={18} aria-hidden="true" /> Open on phone
        </h2>
        <svg
          className="phone-qr"
          viewBox={`${-MARGIN} ${-MARGIN} ${box} ${box}`}
          role="img"
          aria-label={`QR code for ${url}`}
          shapeRendering="crispEdges"
        >
          <rect x={-MARGIN} y={-MARGIN} width={box} height={box} fill="#fff" />
          <path d={d} fill="#000" />
        </svg>
        <div className="phone-url">
          <code>{url}</code>
          <button
            type="button"
            className="icon-btn"
            aria-label={copied ? "Link copied" : "Copy link"}
            title={copied ? "Link copied" : "Copy link"}
            onClick={() => void copyText(url, browserClipboardEnv()).then((outcome) => setCopied(outcome === "copied"))}
          >
            {copied ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />}
          </button>
        </div>
        <div className="modal-actions">
          <button type="button" className="primary-btn" autoFocus onClick={finish}>
            Done
          </button>
        </div>
      </div>
    </dialog>
  );
}
