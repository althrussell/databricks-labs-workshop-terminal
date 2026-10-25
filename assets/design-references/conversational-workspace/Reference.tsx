import { useState } from "react";
import type { FormEvent } from "react";

/** Conversational workspace reference: question entry and evidence stay usable during follow-ups. */
export type ChatExample = "default" | "long-label" | "empty" | "loading" | "error";
export const chatExamples: ChatExample[] = ["default", "long-label", "empty", "loading", "error"];
export type Turn = { id: string; question: string; answer: string; evidence: string };

const SAMPLE: Turn[] = [
  { id: "t1", question: "Which feeders have the most reduced export limits?", answer: "F-14B and F-22 (sample answer).",
    evidence: "SELECT feeder, count(*) FROM applications WHERE export_limit = 'reduced' GROUP BY feeder" },
];

export function ConversationalWorkspaceReference({ example = "default", onAsk }: {
  example?: ChatExample;
  /** Resolves with the agent's answer and its evidence; rejects with why it could not answer. */
  onAsk?: (question: string) => Promise<{ answer: string; evidence: string }>;
}) {
  const [turns, setTurns] = useState<Turn[]>(example === "default" || example === "long-label" ? SAMPLE : []);
  const [selected, setSelected] = useState<string | null>(turns[0]?.id ?? null);
  const [draft, setDraft] = useState(example === "error" ? "Which feeders are full?" : "");
  const [busy, setBusy] = useState(example === "loading");
  const [problem, setProblem] = useState(example === "error" ? "The agent did not answer. Your question is kept; ask again." : "");
  const current = turns.find((turn) => turn.id === selected) ?? null;

  async function ask(event: FormEvent) {
    event.preventDefault();
    const question = draft.trim();
    if (!question || busy) return;
    setBusy(true);
    setProblem("");
    try {
      if (!onAsk) throw new Error("The agent is not connected yet, so nothing was asked.");
      const result = await onAsk(question);
      const turn = { id: `t${turns.length + 1}`, question, ...result };
      setTurns([...turns, turn]);
      setSelected(turn.id);
      setDraft("");
    } catch (error) {
      setProblem(error instanceof Error ? error.message : "The agent did not answer.");
    } finally {
      setBusy(false);
    }
  }

  return <div className="grid h-full gap-4 p-4 md:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
    <section aria-label="Conversation" className="flex min-h-0 flex-col">
      <ol aria-live="polite" className="flex-1 space-y-3 overflow-y-auto">
        {turns.length === 0 && !busy ? <li className="text-sm text-muted-foreground">Ask about your data, for example “Which feeders are closest to full?”</li> : null}
        {turns.map((turn) => <li key={turn.id}>
          <button type="button" aria-pressed={turn.id === selected} onClick={() => setSelected(turn.id)}
            className="w-full rounded border border-border p-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <span className="block text-sm font-medium" title={turn.question}>
              {example === "long-label" ? `${turn.question} Include the regional breakdown and the months since the last upgrade.` : turn.question}
            </span>
            <span className="block text-sm">{turn.answer}</span>
          </button>
        </li>)}
        {busy ? <li role="status" className="text-sm text-muted-foreground">Answering…</li> : null}
      </ol>
      <form onSubmit={(e) => void ask(e)} className="mt-3 flex gap-2" aria-label="Ask a question">
        <label htmlFor="question" className="sr-only">Question</label>
        <input id="question" value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="Ask a follow-up"
          className="min-w-0 flex-1 rounded border border-border bg-background px-2 py-1.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
        <button type="submit" disabled={busy || !draft.trim()}
          className="rounded bg-primary px-3 py-1.5 text-primary-foreground disabled:opacity-60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Ask</button>
      </form>
      {problem ? <p role="alert" className="mt-2 text-sm text-destructive">{problem}</p> : null}
    </section>
    <aside aria-label="Evidence for the selected answer" className="rounded border border-border p-3">
      {current ? <><h2 className="text-sm font-semibold">Evidence</h2>
        <pre className="mt-2 whitespace-pre-wrap break-words font-mono text-xs">{current.evidence}</pre></>
        : <p className="text-sm text-muted-foreground">Select an answer to see where it came from.</p>}
    </aside>
  </div>;
}
