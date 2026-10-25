import { useState } from "react";

/** Learning interaction reference: one next step, feedback grounded in the actual answer, progress from completed work. */
export type LearnExample = "default" | "long-label" | "empty" | "loading" | "error";
export const learnExamples: LearnExample[] = ["default", "long-label", "empty", "loading", "error"];
export type Exercise = { id: string; prompt: string; check: (answer: string) => { correct: boolean; why: string } };

const SAMPLE: Exercise[] = [
  { id: "e1", prompt: "Write the SQL keyword that keeps only rows matching a condition.",
    check: (answer) => answer.trim().toUpperCase() === "WHERE"
      ? { correct: true, why: "WHERE filters rows before they are grouped." }
      : { correct: false, why: `“${answer.trim() || "(blank)"}” does not filter rows. Look for the clause that comes after FROM.` } },
  { id: "e2", prompt: "Which keyword filters groups after GROUP BY?",
    check: (answer) => answer.trim().toUpperCase() === "HAVING"
      ? { correct: true, why: "HAVING filters groups, after aggregation." }
      : { correct: false, why: "That keyword works on rows. Groups need the clause that runs after aggregation." } },
];

export function LearningInteractionReference({ example = "default" }: { example?: LearnExample }) {
  const exercises = example === "empty" ? [] : SAMPLE;
  const [index, setIndex] = useState(0);
  const [answer, setAnswer] = useState("");
  const [feedback, setFeedback] = useState<{ correct: boolean; why: string } | null>(
    example === "error" ? { correct: false, why: "Checking failed. Your answer is kept; check again." } : null);
  const [done, setDone] = useState<string[]>([]);

  if (example === "loading") return <div role="status" aria-label="Loading exercises" className="m-4 h-32 animate-pulse rounded bg-muted" />;
  if (exercises.length === 0) return <p className="m-4 text-sm text-muted-foreground">No exercises yet. Ask your coach for a first one about your own data.</p>;
  const exercise = exercises[Math.min(index, exercises.length - 1)];
  const finished = done.length === exercises.length;

  return <div className="max-w-xl space-y-4 p-4">
    <p className="text-sm text-muted-foreground">{done.length} of {exercises.length} exercises done</p>
    {finished ? <p role="status" className="font-medium">All done. Next, try the same idea on your own table.</p> : <>
      <label htmlFor="answer" className="block font-medium">
        {example === "long-label" ? `${exercise.prompt} Think about the order in which the query engine applies each clause.` : exercise.prompt}
      </label>
      <input id="answer" value={answer} onChange={(e) => setAnswer(e.target.value)}
        className="w-full rounded border border-border bg-background px-2 py-1.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
      <div className="flex gap-2">
        <button type="button" onClick={() => setFeedback(exercise.check(answer))}
          className="rounded bg-primary px-3 py-1.5 text-primary-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Check</button>
        {feedback?.correct ? <button type="button" onClick={() => { setDone([...done, exercise.id]); setIndex(index + 1); setAnswer(""); setFeedback(null); }}
          className="rounded border border-border px-3 py-1.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Next exercise</button> : null}
      </div>
      {feedback ? <p role="status" className={feedback.correct ? "text-sm text-success" : "text-sm text-destructive"}>{feedback.why}</p> : null}
    </>}
  </div>;
}
