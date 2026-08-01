# Composition and layout

Composition is decided before components. A page assembled out of whatever
primitives were nearest reads as a framework starter no matter how good the
primitives are.

## Give the page one job

Name the single thing this screen exists to do — see what is late, play the
game, compare this quarter to last, get the data in. Everything else on the
page is subordinate to it. If you cannot name it, the page is not designed yet.

The primary task should be obvious within about five seconds of the page
loading, without reading anything carefully.

## Choose the grammar, do not default to cards

A wall of identical cards is the default output of every component library and
the clearest sign nobody chose anything. Cards are correct when content is
genuinely modular and comparable — repeated entities of the same kind.

Otherwise pick the grammar the content actually has:

| Content | Grammar |
|---|---|
| A pitch or first-run screen | Full-bleed sections with real vertical scale |
| One work surface, focused | A single dominant pane, controls to one side |
| Comparable metrics | A tight row of values, aligned on the numbers |
| Investigation across records | A dense table with a detail pane |
| A game or canvas | The canvas centred and dominant, chrome minimal |
| A sequence of steps | A vertical narrative with progressive disclosure |

## Hierarchy comes from contrast, not from boxes

Three levers, in order of strength: **size**, **weight**, and **position**.
Colour is fourth and much weaker than people expect — a heading is not
important because it is blue.

Something on the page should be clearly the largest thing. If the biggest
element on screen is a card border, the hierarchy is inverted.

Deliberate emptiness is a hierarchy tool. Space around an element promotes it
more reliably than decorating the element does.

## Spacing rhythm

Pick one spacing scale and use it everywhere. A 4px base (4, 8, 12, 16, 24, 32,
48, 64, 96) is enough. The failure mode is not choosing the wrong scale, it is
using values off the scale in a few places and reading as slightly wrong
everywhere.

Space between groups should be visibly larger than space within a group — that
gap is what communicates structure. Generous outer padding on a page is what
separates a designed layout from a template; default container padding is
almost always too tight.

Constrain line length for reading text (roughly 60–75 characters). Full-width
paragraphs on a wide screen are unreadable regardless of the type.

## Responsive means transformation, not shrinking

A layout that only gets narrower is a desktop layout being squeezed. Decide what
each breakpoint is *for*:

- **Narrow** — one column, the primary task first, secondary content below or
  behind disclosure. Touch targets at least 44px.
- **Medium** — two columns where content genuinely pairs.
- **Wide** — use the extra width for comparison or context, not for stretching
  a single column of text across the screen.

Check the layout survives a narrow window before you deploy: no horizontal
overflow, nothing clipped, nothing overlapping.

## Real content, always

Build with plausible real content from the start — real labels, realistic value
lengths, real empty cases. Lorem ipsum and three-character sample values hide
every layout problem the app will actually have, and "we'll fix the copy later"
means shipping a screen that reads as unfinished.

Never fabricate metrics, testimonials, or customer logos to fill space. If a
number is not real, the screen should say so or not show it.
