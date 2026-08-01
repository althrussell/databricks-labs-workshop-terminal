# AppKit patterns

Copy-ready code that already clears the visual baseline. **Adapt one of these
rather than composing a screen from primitives** — it is faster than inventing,
and it is why the quality floor holds when the clock is running.

| File | Use it for |
|---|---|
| `app-shell.tsx` | The frame: navigation, wordmark, page header, content region |
| `hero-first-run.tsx` | The screen an app shows before it has any content |
| `kpi-row.tsx` | A row of headline numbers with honest deltas |
| `chart-card.tsx` | A chart with a title, a plain-language read, and provenance |
| `data-table.tsx` | `DataTable` for query results; primitives for in-memory rows |
| `states.tsx` | Loading, empty, no-results, and error — plus the wiring |
| `form.tsx` | Input with real validation, error, and submitting states |

## How to use them

Change the content, the copy, the colour, and the composition to suit the
product. Keep the structural decisions: the type scale, the spacing rhythm, the
alignment of numbers, the focus handling, and the presence of every state.

They are a floor, not a ceiling. Starting above them is the goal; starting
below them is the failure this library exists to prevent.

The sample domain (a delivery fleet) is only there to make the code concrete.
Replace it entirely.

## What is verified, and what you still check

Every component and prop used here is verified against the published
`@databricks/appkit-ui` package — the files typecheck under `strict` with no
errors, so nothing in them is an invented component or a guessed prop name.

Two things are still on you:

1. **The installed version may differ.** These were authored against
   `@databricks/appkit-ui` 0.50.0. If a prop is rejected, confirm the real
   signature with `npx @databricks/appkit docs "appkit-ui API reference"` rather
   than casting around it. Never write `as unknown as <T>`.
2. **Query-mode components need real queries.** `queryKey` values here
   (`on_time_by_week`, `recent_deliveries`) are illustrative. Point them at your
   own `config/queries/*.sql`, and remember `parameters` is required even when
   the query takes none.

## The boundary

These patterns own composition, spacing, type, and states.

`databricks-app-design` owns what happens *inside* a data surface — which chart
type, which scale, semantic colour for encoding, KPI notation, and Genie trust.
`chart-card.tsx` deliberately frames a chart without choosing it. On any
chart-vocabulary conflict, that skill wins.
