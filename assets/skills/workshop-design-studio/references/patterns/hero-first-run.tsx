/**
 * First-run / hero — the screen for an app with nothing in it yet.
 *
 * This is the first thing the attendee sees when they open their URL, so it is
 * the highest-leverage screen in the app and the one most often left as "No
 * data". Give it a real display size, one clear action, and enough character
 * that opening the link is a reveal.
 *
 * Why it looks designed:
 *  - a genuinely large display type size (text-5xl/text-6xl), not a big-ish h1;
 *  - a soft token-based gradient wash for depth instead of a flat grey box;
 *  - one primary action, with a quiet secondary beside it;
 *  - the supporting line says what will be here, not "no data".
 */
import { Button } from '@databricks/appkit-ui/react';
import { ArrowRight, Route } from 'lucide-react';

export function HeroFirstRun({ onStart }: { onStart?: () => void }) {
  return (
    <section className="relative overflow-hidden rounded-2xl border border-border bg-card">
      {/* Depth from a soft wash of the accent, not from a hard drop shadow. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-gradient-to-br from-primary/10 via-transparent to-transparent"
      />

      <div className="relative mx-auto max-w-2xl px-8 py-20 text-center sm:py-28">
        <span className="inline-flex size-14 items-center justify-center rounded-2xl bg-primary/10 text-primary">
          <Route className="size-7" aria-hidden="true" />
        </span>

        <h1 className="mt-8 text-5xl font-semibold tracking-tight sm:text-6xl">
          Every delivery,
          <br />
          on one screen.
        </h1>

        <p className="mx-auto mt-5 max-w-md text-base leading-relaxed text-muted-foreground">
          Add your first vehicle and this becomes a live map of what is moving,
          what is late, and who to call about it.
        </p>

        <div className="mt-9 flex items-center justify-center gap-3">
          <Button size="lg" onClick={onStart}>
            Add your first vehicle
            <ArrowRight className="size-4" aria-hidden="true" />
          </Button>
          <Button size="lg" variant="ghost">
            See an example
          </Button>
        </div>
      </div>
    </section>
  );
}
