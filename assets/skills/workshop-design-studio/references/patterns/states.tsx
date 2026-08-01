/**
 * Loading, empty, and error states.
 *
 * These three are what separate a product from a demo, and they are the first
 * thing dropped under time pressure. Copy them; they cost nothing.
 *
 * AppKit's own data components (charts, DataTable) already handle their
 * internal loading and error states — build these for content you fetch or
 * compute yourself.
 *
 * `RemoteSection` at the bottom shows the whole set wired together, including
 * the distinction that gets missed most: "nothing yet" and "nothing matched
 * your filter" are different screens, and both differ from "we could not load
 * it".
 */
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Button,
  Card,
  CardContent,
  CardHeader,
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
  Skeleton,
} from '@databricks/appkit-ui/react';
import { AlertTriangle, Plus, SearchX, Truck } from 'lucide-react';

/**
 * Loading — a skeleton in the shape of the content that is coming, so the
 * layout does not jump when data lands. Never a spinner on an empty page, and
 * never the text "Loading...".
 */
export function LoadingState() {
  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4" aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading fleet summary</span>
      {[0, 1, 2, 3].map((i) => (
        <Card key={i}>
          <CardHeader className="pb-2">
            <Skeleton className="h-3 w-24" />
            <Skeleton className="mt-2 h-9 w-20" />
          </CardHeader>
          <CardContent>
            <Skeleton className="h-4 w-32" />
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

/**
 * Empty — nothing here *yet*. Say what will fill this space, why it is empty,
 * and give the one action that fills it. "No data" is a wasted screen.
 */
export function EmptyState({ onAdd }: { onAdd?: () => void }) {
  return (
    <Empty className="rounded-xl border border-dashed border-border py-16">
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <Truck className="size-6" aria-hidden="true" />
        </EmptyMedia>
        <EmptyTitle>No vehicles yet</EmptyTitle>
        <EmptyDescription>
          Add a vehicle and its trips, delays, and driver show up here the moment
          it starts moving.
        </EmptyDescription>
      </EmptyHeader>
      <EmptyContent>
        <Button onClick={onAdd}>
          <Plus className="size-4" aria-hidden="true" />
          Add a vehicle
        </Button>
      </EmptyContent>
    </Empty>
  );
}

/**
 * No results — different from empty. There *is* data; this filter just did not
 * match any of it. The action is to clear the filter, not to create something.
 */
export function NoResultsState({ query, onClear }: { query: string; onClear?: () => void }) {
  return (
    <Empty className="py-12">
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <SearchX className="size-6" aria-hidden="true" />
        </EmptyMedia>
        <EmptyTitle>No deliveries match “{query}”</EmptyTitle>
        <EmptyDescription>Try a shorter search, or clear the filter to see everything.</EmptyDescription>
      </EmptyHeader>
      <EmptyContent>
        <Button variant="outline" onClick={onClear}>
          Clear filter
        </Button>
      </EmptyContent>
    </Empty>
  );
}

/**
 * Error — say what failed in the user's terms and what they can do. Never
 * render a raw exception as the user-facing text, and never fail silently into
 * an empty state: "there is nothing" and "we could not load it" are different
 * facts.
 */
export function ErrorState({ onRetry }: { onRetry?: () => void }) {
  return (
    <Alert variant="destructive">
      <AlertTriangle className="size-4" aria-hidden="true" />
      <AlertTitle>We couldn’t load your fleet</AlertTitle>
      <AlertDescription className="space-y-3">
        <p>
          The connection to the delivery database timed out. Your data is safe —
          this is usually temporary.
        </p>
        <Button size="sm" variant="outline" onClick={onRetry}>
          Try again
        </Button>
      </AlertDescription>
    </Alert>
  );
}

/** The whole set, wired to one async result. */
export function RemoteSection({
  loading,
  error,
  rows,
  filter,
  onRetry,
  onAdd,
  onClear,
}: {
  loading: boolean;
  error: unknown;
  rows: unknown[];
  filter: string;
  onRetry?: () => void;
  onAdd?: () => void;
  onClear?: () => void;
}) {
  if (loading) return <LoadingState />;
  if (error) return <ErrorState onRetry={onRetry} />;
  if (rows.length === 0 && filter) return <NoResultsState query={filter} onClear={onClear} />;
  if (rows.length === 0) return <EmptyState onAdd={onAdd} />;
  return <div className="grid gap-4">{/* the real content */}</div>;
}
