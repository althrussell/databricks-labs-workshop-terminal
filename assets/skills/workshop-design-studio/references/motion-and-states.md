# Motion and states

The states are what separate a demo from a product. Most apps look unfinished
not because the happy path is ugly but because everything else is missing.

## Every asynchronous thing has three other states

For anything that loads, fetches, submits, or can fail:

**Loading** — use `Skeleton` in the shape of the content that is coming, not a
spinner in the middle of an empty page and never the text "Loading...". The
skeleton should occupy the same space as the result so the layout does not jump
when data lands. AppKit's chart and `DataTable` components handle their own
loading state; you only build this for content you fetch yourself.

**Empty** — the highest-leverage screen in the whole app, because it is the
first thing an attendee sees and the one everyone leaves as a shrug. Use
`Empty` with `EmptyMedia`, `EmptyTitle`, `EmptyDescription`, and
`EmptyContent`. Say what this space will hold, why it is empty, and give the
single action that fills it. "No data" is a wasted screen.

Distinguish *nothing yet* from *nothing matched a filter* — they need different
words and different actions.

**Error** — use `Alert` with `variant="destructive"`. Say what failed in the
user's terms and what they can do. Never render a raw stack trace or an
exception message as the user-facing text, and never fail silently into an
empty state; "there is no data" and "we could not load the data" are different
facts.

## Interaction states on everything interactive

Every control needs hover, focus-visible, active, and disabled. Focus is the
one that gets dropped and the one that matters most — keyboard users have
nothing else to navigate by.

Never remove the focus ring. If the default does not suit the design, restyle
it (`focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2`)
so it is clearly visible against its background. `outline: none` with no
replacement makes the app unusable without a mouse.

Disabled controls should look unmistakably inert and, where the reason is not
obvious, say why.

## Forms

Validate on blur and on submit, not on every keystroke — errors that appear
while someone is still typing their first character read as hostile.

Put the error next to the field it belongs to, describe the fix rather than the
violation ("use a work email address", not "invalid input"), and mark required
fields once, consistently. On submit, disable the button and show progress in
place so nobody clicks twice.

## Motion

Motion exists to explain a change: something appeared, something moved,
something is working. Motion that plays because the page loaded is decoration
and it ages badly.

- 150–200ms for small state changes, 250–350ms for entrances and layout shifts.
- Ease-out for things arriving, ease-in for things leaving.
- Animate `transform` and `opacity`. Animating layout properties is what makes
  an app feel janky.
- One choreographed moment per app is plenty.

**Always honour reduced motion.** Wrap non-essential animation:

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
```

The state change itself must still happen — reduced motion removes the
animation, never the feedback.

## Feedback on every action

Anything the user triggers gets a visible response within about 100ms, even if
the work takes longer: a pressed state, a spinner in the button, an optimistic
update, or a toast on completion. Silence after a click is indistinguishable
from a broken app, and it is the fastest way to make good work feel unreliable.
