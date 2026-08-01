# Colour, tokens, and theming a Databricks App

AppKit ships a token-based theme. Style through the tokens, and both light and
dark mode work for free. Style around them with raw values and you get an app
that looks fine in one mode and broken in the other.

## Use tokens, never raw colour

Never write hex values (`#22c55e`) or raw Tailwind palette utilities
(`bg-amber-100`, `text-emerald-600`, `fill-red-500`) in components. Both bypass
the theme and break dark mode.

The tokens AppKit's stylesheet defines, and the utilities that reach them:

| Intent | Token | Utilities |
|---|---|---|
| Page surface | `--background` / `--foreground` | `bg-background`, `text-foreground` |
| Raised surface | `--card` / `--card-foreground` | `bg-card`, `text-card-foreground` |
| Brand / primary action | `--primary` / `--primary-foreground` | `bg-primary`, `text-primary` |
| Secondary action | `--secondary` | `bg-secondary` |
| Supporting text, quiet fills | `--muted` / `--muted-foreground` | `text-muted-foreground`, `bg-muted` |
| Hover / subtle highlight | `--accent` | `bg-accent` |
| Good, healthy, up-is-good | `--success` | `text-success`, `bg-success` |
| Caution | `--warning` | `text-warning`, `bg-warning` |
| Error, breach, destructive | `--destructive` | `text-destructive`, `bg-destructive` |
| Borders and dividers | `--border` | `border-border` |
| Focus ring | `--ring` | `ring-ring` |
| Corner radius | `--radius` | `rounded-lg`, `rounded-md` |

Charts have their own tokens (`--chart-grid`, `--chart-axis-label`,
`--chart-axis-title`, `--chart-tooltip-bg`) and palette variables. Pass
`colorPalette` to a chart rather than a `colors` array unless the product's
brand genuinely requires specific hues.

## Rebrand by overriding tokens, not components

To give the app its own identity, override the token values once in the app's
stylesheet and let every component follow:

```css
:root {
  --primary: oklch(0.55 0.19 264);
  --primary-foreground: oklch(0.99 0 0);
  --radius: 0.75rem;
}

.dark {
  --primary: oklch(0.72 0.16 264);
  --primary-foreground: oklch(0.16 0.02 264);
}
```

This is the whole theming story. Editing individual components to hardcode a
brand colour means the next component you add does not match, and dark mode
diverges immediately.

## One accent, and it must mean something

Pick one accent and spend it on meaning: the primary action, the live value,
the thing that changed, the state that needs attention. An accent applied to
every heading, border, and icon stops carrying information and becomes
decoration — which reads as less designed, not more.

A useful proportion: most of the screen is background, surface, and text;
colour is a small fraction of it. If you cannot point at what the colour is
telling the user, remove it.

## Semantic colour is not decorative colour

Inside a data surface, colour encodes meaning and the same meaning must always
look the same — green is not "a nice green", it is *good*. That vocabulary
belongs to `databricks-app-design`, and where the two conflict it wins.

Outside data surfaces, colour is expressive and this skill owns it. The
constraint that survives both: **never encode meaning in colour alone.** Pair
it with an icon, a label, or a shape, or it is invisible to a colour-blind user
and to anyone printing the page.

## Contrast is not optional

Body and UI text needs at least 4.5:1 against its background; large display
text at least 3:1. The combinations that fail most often are muted text on a
tinted surface, white text on a mid-tone accent, and placeholder text.

Check the pairs you invent. The tokens are safe by construction; anything you
introduce is your responsibility, and nothing downstream will catch it.

## Surface and depth

Distinguish layers deliberately. Pick one approach and hold it: either
elevation (shadow) or containment (border and a shifted surface), not both at
random. Shadows should be soft and low-opacity — a hard drop shadow reads as
dated. Borders should be a single hairline weight throughout.

Stock cards on stock grey is the default look. Choosing a slightly warmer or
cooler background, or letting the page surface and the card surface differ
meaningfully, is most of what makes an app feel considered.
