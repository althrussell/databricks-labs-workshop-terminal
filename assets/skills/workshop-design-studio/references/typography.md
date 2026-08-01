# Typography

Type is the cheapest way to make an app look designed and the most common thing
left at defaults. A page where everything is 14–16px is the signature of an
untouched template, and no amount of colour rescues it.

## Build a real scale

Pick a scale with genuine jumps and use only its steps:

| Role | Size | Notes |
|---|---|---|
| Display | 48–72px | Hero or first-run screens. Be braver than feels right. |
| Page title | 30–36px | The one thing the page is about. |
| Section | 20–24px | Groups within a page. |
| Body | 15–16px | Never smaller for primary reading. |
| Supporting | 13–14px | Labels, captions, metadata. |
| Metric | 36–56px | Numbers people came to read. Tabular figures. |

The gap between the largest and smallest text on a screen should be obvious at
a glance. If your title is 18px and your body is 16px, you have a document, not
an interface.

## Weight and colour do the rest

Two weights are usually enough — a regular for body and a semibold or bold for
emphasis. Reserve the heaviest weight for genuinely rare emphasis; if half the
page is bold, none of it is.

Use `text-muted-foreground` for supporting text rather than shrinking it. Weak
contrast at a readable size beats full contrast at an unreadable one — but do
not go below 4.5:1 to achieve it.

## Line height and measure

- Display and titles: tight, roughly 1.1–1.25. Large text at 1.5 looks loose
  and unconsidered.
- Body: 1.5–1.6.
- Cap reading text at roughly 60–75 characters per line.

## Numbers

Anything a person will compare or scan — metrics, table columns, currency —
uses tabular figures (`font-variant-numeric: tabular-nums`, or the
`tabular-nums` utility) and is right-aligned in tables. Digits that shift width
between renders make a live value look broken.

Format numbers for a reader, not for a database: thousands separators, sensible
precision, and the unit stated once rather than repeated in every row.

## Font choice

The AppKit default stack is fine and costs nothing. If you choose something
else, choose deliberately and load it properly — a font that flashes or fails
to load looks worse than the default.

Pairing that works without thought: one distinctive face for display, a neutral
workhorse for body and UI. Do not use more than two families, and do not use a
display face for body text.

## Copy is part of the typography

Type quality cannot save bad copy. Headings should say something specific
("3 orders are late") rather than name a component ("Orders"). Buttons should
name the action ("Create order"), never "Submit". Sentence case reads as
modern; Title Case On Everything reads as dated.
