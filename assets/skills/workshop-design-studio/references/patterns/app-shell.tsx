/**
 * App shell — navigation, page header, content region.
 *
 * The frame every multi-screen app sits in. Copy it, rename it, change the nav
 * items and the accent; do not rebuild it from primitives.
 *
 * Why it looks designed rather than defaulted:
 *  - the page title is genuinely large (text-3xl) against 14px nav, so the
 *    hierarchy is visible before anything is read;
 *  - outer padding is generous (px-8 py-10) instead of the default cramped p-4;
 *  - the active nav item is marked with weight and a bar, not colour alone;
 *  - every interactive element has a visible focus ring.
 */
import { useState } from 'react';
import { Button, Separator } from '@databricks/appkit-ui/react';
import { Activity, LayoutGrid, Settings } from 'lucide-react';

const NAV = [
  { id: 'overview', label: 'Overview', icon: LayoutGrid },
  { id: 'activity', label: 'Activity', icon: Activity },
  { id: 'settings', label: 'Settings', icon: Settings },
];

export function AppShell({ children }: { children?: React.ReactNode }) {
  const [active, setActive] = useState('overview');

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-7xl items-center gap-8 px-8">
          {/* Wordmark. A confident wordmark beats an improvised logo. */}
          <span className="py-4 text-base font-semibold tracking-tight">
            Fleet<span className="text-primary">.</span>
          </span>

          <nav className="flex items-center gap-1" aria-label="Main">
            {NAV.map(({ id, label, icon: Icon }) => {
              const isActive = id === active;
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => setActive(id)}
                  aria-current={isActive ? 'page' : undefined}
                  className={[
                    'relative flex items-center gap-2 px-3 py-4 text-sm transition-colors',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                    'focus-visible:ring-offset-2 focus-visible:ring-offset-card',
                    isActive
                      ? 'font-semibold text-foreground'
                      : 'font-medium text-muted-foreground hover:text-foreground',
                  ].join(' ')}
                >
                  <Icon className="size-4" aria-hidden="true" />
                  {label}
                  {/* Weight carries the state; the bar reinforces it. Never colour alone. */}
                  {isActive && (
                    <span className="absolute inset-x-2 -bottom-px h-0.5 rounded-full bg-primary" />
                  )}
                </button>
              );
            })}
          </nav>

          <div className="ml-auto">
            <Button size="sm">New vehicle</Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-8 py-10">
        {/* Page header. The title is the largest thing on the page, by a lot. */}
        <div className="flex items-start justify-between gap-6">
          <div className="space-y-1">
            <h1 className="text-3xl font-semibold tracking-tight">Fleet overview</h1>
            <p className="max-w-prose text-sm text-muted-foreground">
              Six vehicles are on the road right now and two need attention today.
            </p>
          </div>
        </div>

        <Separator className="my-8" />

        {/* Content region — space between groups is larger than space within one. */}
        <div className="space-y-10">{children}</div>
      </main>
    </div>
  );
}
