import { useEffect, useState } from "react";
import { HandHelping } from "lucide-react";
import { api, HelpState } from "../api";
import { onAppEvent } from "../events";

const NOTE_MAX = 280;

export default function RaiseHandButton({ initial }: { initial: HelpState | null }) {
  const [raised, setRaised] = useState(initial?.raised ?? false);
  const [noteOpen, setNoteOpen] = useState(false);
  const [note, setNote] = useState(initial?.note ?? "");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (initial) {
      setRaised(initial.raised);
      setNote(initial.note ?? "");
    }
  }, [initial]);

  useEffect(() => {
    return onAppEvent((event) => {
      if (event.t === "broadcast" && event.clear_help) {
        setRaised(false);
        setNote("");
        setNoteOpen(false);
      }
    });
  }, []);

  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    try {
      if (raised) {
        const result = await api.lowerHelp();
        setRaised(result.raised);
        setNote("");
        setNoteOpen(false);
      } else if (noteOpen && note.trim()) {
        const result = await api.raiseHelp(note.trim());
        setRaised(result.raised);
        setNote(result.note ?? note.trim());
        setNoteOpen(false);
      } else if (!noteOpen) {
        setNoteOpen(true);
      } else {
        const result = await api.raiseHelp();
        setRaised(result.raised);
        setNoteOpen(false);
      }
    } catch {
      /* fail-soft: local state still updates when the server accepts */
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="raise-hand-wrap">
      {noteOpen && !raised ? (
        <div className="raise-hand-note">
          <input
            type="text"
            value={note}
            maxLength={NOTE_MAX}
            placeholder="Optional note for the operator"
            onChange={(e) => setNote(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void toggle();
              if (e.key === "Escape") setNoteOpen(false);
            }}
            autoFocus
          />
          <button className="phase-btn" type="button" onClick={() => setNoteOpen(false)}>
            Cancel
          </button>
          <button className="primary-btn" type="button" disabled={busy} onClick={() => void toggle()}>
            Raise
          </button>
        </div>
      ) : (
        <button
          type="button"
          className={`raise-hand-btn ${raised ? "raise-hand-active" : ""}`}
          disabled={busy}
          onClick={() => void toggle()}
          title={
            raised
              ? "Lower your hand — the operator has been notified"
              : "Raise your hand for operator help"
          }
        >
          <HandHelping size={14} />
          {raised ? "Hand raised" : "Raise hand"}
        </button>
      )}
    </div>
  );
}
