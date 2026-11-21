import { Keyboard, Rocket } from "lucide-react";
import type { SessionInfo } from "../api";
import ImageDropZone from "./ImageDropZone";

interface Props {
  session: SessionInfo;
  buildName: string;
  launching: boolean;
  inputDisabled: boolean;
  attachBusy: boolean;
  attached: { url: string } | null;
  onFiles: (files: File[]) => void;
  onRelaunch: () => void;
  /** Focus xterm itself, for the rare raw-typing case (vim, a y/n prompt). */
  onTypeInTerminal: () => void;
}

/** One slim row above the phone terminal: which agent, which build, and the two
 * actions that belong next to the terminal. Everything else is on the Build tab. */
export default function PhoneStatusRow({
  session,
  buildName,
  launching,
  inputDisabled,
  attachBusy,
  attached,
  onFiles,
  onRelaunch,
  onTypeInTerminal,
}: Props) {
  const where = buildName ? `${session.label} · ${buildName}` : session.label;
  return (
    <div className="phone-status">
      <span className="phone-capsule" title={session.exited ? `${session.label} has ended` : `${session.label} is running`}>
        <span className={`dot ${session.exited ? "dot-warn" : "dot-online"}`} aria-hidden="true" />
        <span className="phone-capsule-text">{where}</span>
      </span>
      {session.exited ? (
        <button type="button" className="primary-btn phone-relaunch" title="Relaunch" disabled={launching} onClick={onRelaunch}>
          <Rocket size={14} aria-hidden="true" /> Relaunch
        </button>
      ) : (
        <span className="phone-status-actions">
          {attached && (
            <span className="attach-confirm" role="status" aria-live="polite">
              <img src={attached.url} alt="Image attached" title="Image attached" className="attach-thumb" />
            </span>
          )}
          <ImageDropZone
            busy={attachBusy}
            disabled={inputDisabled}
            title={inputDisabled ? "Attaching images works while the workshop is open" : "Attach a screenshot to the prompt"}
            onFiles={onFiles}
          />
          <button
            type="button"
            className="icon-btn"
            aria-label="Type in the terminal"
            title="Type in the terminal"
            disabled={inputDisabled}
            onClick={onTypeInTerminal}
          >
            <Keyboard size={18} aria-hidden="true" />
          </button>
        </span>
      )}
    </div>
  );
}
