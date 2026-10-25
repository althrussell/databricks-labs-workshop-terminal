import { useEffect, useId, useRef, useState } from "react";
import { api, AttendeeProfile, ExplanationDetail, WorkingApproach } from "../api";
import { changesFrom, Choices, choicesFrom, Vocabulary } from "../preferences";
import { trapDialogTab } from "../dialog";

/** Independent optional preferences: changing them never changes the saved task. */
export default function HelpPreference() {
  const id = useId();
  const [profile, setProfile] = useState<AttendeeProfile | null>(null);
  const [choices, setChoices] = useState<Choices>({ approach: "co_build", detail: "", vocabulary: "" });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const active = useRef(true);

  async function load() {
    setError(""); setBusy(true);
    try {
      const value = await api.profile();
      if (active.current) { setProfile(value); setChoices(choicesFrom(value)); }
    } catch (caught) {
      if (active.current) setError(caught instanceof Error ? caught.message : String(caught));
    } finally { if (active.current) setBusy(false); }
  }
  useEffect(() => { active.current = true; void load(); return () => { active.current = false; }; }, []);

  const unchanged = profile ? Object.keys(changesFrom(profile, choices)).length === 1 : true;
  async function save() {
    if (!profile) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const saved = await api.saveProfile(changesFrom(profile, choices));
      if (active.current) {
        setProfile(saved); setChoices(choicesFrom(saved));
        setNotice("Saved. Your task stays the same. New agent sessions use it straight away; an open agent "
          + "picks it up when it next rereads its instructions.");
      }
    } catch (caught) {
      if (active.current) setError(caught instanceof Error ? caught.message : String(caught));
    } finally { if (active.current) setBusy(false); }
  }
  const set = (patch: Partial<Choices>) => { setChoices({ ...choices, ...patch }); setNotice(""); };
  return <div className="wizard-field help-preference">
    <label className="wizard-field-label" htmlFor={`${id}-approach`}>How would you like your coding agent to work with you?</label>
    <p className="wizard-industry-note">Optional, and you can change this anytime. You can also just tell your agent.</p>
    <select id={`${id}-approach`} className="wizard-other-input" value={choices.approach} disabled={!profile || busy}
      onChange={(event) => set({ approach: event.target.value as WorkingApproach })}>
      <option value="co_build">Build with me (explain key decisions and let me steer)</option>
      <option value="learn">Teach me while we build (small hands-on steps)</option>
      <option value="lead">Let me drive (I set constraints and choose what you build)</option>
    </select>
    <label className="wizard-field-label" htmlFor={`${id}-detail`}>Explanation detail</label>
    <select id={`${id}-detail`} className="wizard-other-input" value={choices.detail} disabled={!profile || busy}
      onChange={(event) => set({ detail: event.target.value as ExplanationDetail })}>
      <option value="">Adapt to my request</option><option value="brief">Brief</option><option value="detailed">Detailed</option>
    </select>
    <label className="wizard-field-label" htmlFor={`${id}-vocabulary`}>Language</label>
    <select id={`${id}-vocabulary`} className="wizard-other-input" value={choices.vocabulary} disabled={!profile || busy}
      onChange={(event) => set({ vocabulary: event.target.value as Vocabulary })}>
      <option value="">Follow the topic</option><option value="guided">Plain language</option>
      <option value="technical">Technical names and choices</option>
    </select>
    {profile && <button className="btn btn-ghost" disabled={busy || unchanged} onClick={() => void save()}>
      {busy ? "Saving…" : "Save preferences"}</button>}
    {notice && <p role="status">{notice}</p>}
    {error && <div role="alert"><p>{error}</p><button className="btn btn-ghost" disabled={busy} onClick={() => void load()}>Reload preferences</button></div>}
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
