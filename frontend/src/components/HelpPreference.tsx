import { useEffect, useId, useRef, useState } from "react";
import { api, AttendeeProfile } from "../api";
import { trapDialogTab } from "../dialog";

/** Independent optional preference: changing it never changes the saved task. */
export default function HelpPreference() {
  const id = useId();
  const [profile, setProfile] = useState<AttendeeProfile | null>(null);
  const [choice, setChoice] = useState<AttendeeProfile["help_preference"]>("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const active = useRef(true);

  async function load() {
    setError(""); setBusy(true);
    try {
      const value = await api.profile();
      if (active.current) { setProfile(value); setChoice(value.help_preference); }
    } catch (caught) {
      if (active.current) setError(caught instanceof Error ? caught.message : String(caught));
    } finally { if (active.current) setBusy(false); }
  }
  useEffect(() => { active.current = true; void load(); return () => { active.current = false; }; }, []);

  async function save() {
    if (!profile) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const saved = await api.saveProfile({ expected_revision: profile.revision, help_preference: choice });
      if (active.current) {
        setProfile(saved);
        setNotice("Preference saved. Your task stays the same. New agent turns can use it; ask an open agent to reread its instructions if needed.");
      }
    } catch (caught) {
      if (active.current) setError(caught instanceof Error ? caught.message : String(caught));
    } finally { if (active.current) setBusy(false); }
  }
  return <div className="wizard-field help-preference">
    <label className="wizard-field-label" htmlFor={id}>How should your agent help?</label>
    <p className="wizard-industry-note">Optional. This changes explanation style, and you can change it any time.</p>
    <select id={id} className="wizard-other-input" value={choice} disabled={!profile || busy}
      onChange={(event) => { setChoice(event.target.value as AttendeeProfile["help_preference"]); setNotice(""); }}>
      <option value="">Adapt to my request</option><option value="guided">Guide me</option>
      <option value="concise">Keep it concise</option><option value="technical">Discuss technical choices</option>
    </select>
    {profile && <button className="btn btn-ghost" disabled={busy || (choice === profile.help_preference && profile.source === "attendee")}
      onClick={() => void save()}>{busy ? "Saving…" : "Save preference"}</button>}
    {notice && <p role="status">{notice}</p>}
    {error && <div role="alert"><p>{error}</p><button className="btn btn-ghost" disabled={busy} onClick={() => void load()}>Reload preference</button></div>}
  </div>;
}

export function PreferencesDialog({ onClose }: { onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialog.current?.showModal();
    return () => { dialog.current?.close(); document.body.style.overflow = overflow; if (previous?.isConnected) previous.focus(); };
  }, []);
  return <dialog ref={dialog} className="modal preferences-dialog" aria-labelledby="preferences-title" onCancel={onClose} onKeyDown={trapDialogTab}>
    <h2 id="preferences-title">Agent preferences</h2><HelpPreference />
    <button className="btn btn-primary" onClick={onClose}>Done</button>
  </dialog>;
}
