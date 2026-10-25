import { useRef, useState } from "react";
import type { FormEvent } from "react";

/** Guided form reference: a genuine save, errors next to fields, success only after the outcome. Sample values; replace with the attendee's own. */
export type FormExample = "default" | "long-label" | "empty" | "loading" | "error";
export const formExamples: FormExample[] = ["default", "long-label", "empty", "loading", "error"];
export type Flag = { feeder: string; priority: "high" | "medium" | "low"; reason: string };

export function GuidedFormReference({ example = "default", onSave }: {
  example?: FormExample;
  /** Resolves with the saved time; rejects with the reason it was not saved. */
  onSave?: (flag: Flag) => Promise<string>;
}) {
  const [flag, setFlag] = useState<Flag>(example === "empty" ? { feeder: "", priority: "medium", reason: "" }
    : { feeder: "F-14B", priority: "high", reason: "Applications often get reduced export limits here." });
  const [errors, setErrors] = useState<Partial<Record<keyof Flag, string>>>({});
  const [state, setState] = useState<"idle" | "saving" | "saved" | "failed">(example === "error" ? "failed" : "idle");
  const [message, setMessage] = useState(example === "error" ? "The save did not reach the database. Your entry is kept; try again." : "");
  const feederRef = useRef<HTMLInputElement>(null);
  const reasonRef = useRef<HTMLTextAreaElement>(null);
  const long = example === "long-label";

  if (example === "loading") return <div role="status" aria-label="Loading the flag" className="m-4 h-40 animate-pulse rounded bg-muted" />;

  function validate(next: Flag) {
    const found: Partial<Record<keyof Flag, string>> = {};
    if (!/^F-\d+[A-Z]?$/.test(next.feeder)) found.feeder = "Enter a feeder ID like F-14B.";
    if (next.reason.trim().length < 10) found.reason = "Say why in at least 10 characters, so engineers can act on it.";
    return found;
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const found = validate(flag);
    setErrors(found);
    if (found.feeder) { feederRef.current?.focus(); return; }
    if (found.reason) { reasonRef.current?.focus(); return; }
    setState("saving");
    setMessage("");
    try {
      if (!onSave) throw new Error("Saving is not connected yet, so nothing was saved.");
      const at = await onSave(flag);
      setState("saved");
      setMessage(`Saved ${at}.`);
    } catch (error) {
      setState("failed");
      setMessage(error instanceof Error ? error.message : "Not saved. Your entry is kept; try again.");
    }
  }

  const field = "mt-1 w-full rounded border border-border bg-background px-2 py-1.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
  return <form noValidate onSubmit={(e) => void submit(e)} className="max-w-lg space-y-4 p-4" aria-label="Flag a feeder for upgrade">
    <div>
      <label htmlFor="feeder" className="text-sm font-medium">Feeder</label>
      <input id="feeder" ref={feederRef} value={flag.feeder} onChange={(e) => setFlag({ ...flag, feeder: e.target.value })}
        onBlur={() => setErrors(validate(flag))} aria-invalid={Boolean(errors.feeder)} aria-describedby={errors.feeder ? "feeder-error" : undefined} className={field} />
      {errors.feeder ? <p id="feeder-error" className="mt-1 text-sm text-destructive">{errors.feeder}</p> : null}
    </div>
    <fieldset>
      <legend className="text-sm font-medium">Priority</legend>
      {(["high", "medium", "low"] as const).map((level) => <label key={level} className="mr-4 inline-flex items-center gap-1 text-sm">
        <input type="radio" name="priority" checked={flag.priority === level} onChange={() => setFlag({ ...flag, priority: level })} />
        {long ? `${level} — affects how soon the network planning team schedules the upgrade review` : level}
      </label>)}
    </fieldset>
    <div>
      <label htmlFor="reason" className="text-sm font-medium">Reason</label>
      <textarea id="reason" ref={reasonRef} rows={3} value={flag.reason} onChange={(e) => setFlag({ ...flag, reason: e.target.value })}
        aria-invalid={Boolean(errors.reason)} aria-describedby={errors.reason ? "reason-error" : undefined} className={field} />
      {errors.reason ? <p id="reason-error" className="mt-1 text-sm text-destructive">{errors.reason}</p> : null}
    </div>
    <button type="submit" disabled={state === "saving"}
      className="rounded bg-primary px-4 py-2 text-primary-foreground disabled:opacity-60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
      {state === "saving" ? "Saving…" : state === "saved" ? "Save changes" : "Save flag"}
    </button>
    {message ? <p role={state === "failed" ? "alert" : "status"} className={state === "failed" ? "text-sm text-destructive" : "text-sm text-success"}>{message}</p> : null}
  </form>;
}
