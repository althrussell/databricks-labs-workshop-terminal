---
name: workshop-design-studio
description: 'Mandatory visual-quality skill for ANY interface built in the workshop. Use whenever creating, changing, reviewing, or finishing a Databricks App with a web app, website, dashboard, internal tool, AI experience, game, or other visual UI. Ships copy-ready AppKit patterns plus a non-negotiable visual baseline. Runs autonomously: it never asks the attendee design questions and never explains its own process. Brand-neutral — infer or preserve the product brand instead of imposing Databricks styling. Skip only for backend-only, infrastructure-only, or non-visual tasks.'
metadata:
  version: 4.0.0
  workshop_default: true
  design_scope: databricks-apps
---

# Workshop Design Studio

Every interface an attendee leaves with should look like a senior product
designer and frontend engineer worked on it. Not "clean UI" — a coherent,
credible product that reads as intentionally designed.

Attendees are not designers, and most are not engineers. They will not ask for
this and should never be made to think about it. You do the work; they get the
result.

**This skill is for Databricks Apps built with AppKit.** That is the only
target. There is no stack detection, no multi-framework support, and no
non-AppKit branch — if you are building an interface in this workshop, it is an
AppKit app.

## Start from a pattern, not from scratch

`references/patterns/` holds copy-ready AppKit code that already clears the
baseline below: app shell, first-run hero, KPI row, chart card, data table,
loading/empty/error states, and forms. Every component in them is verified
against the published `@databricks/appkit-ui` package.

**Adapt a pattern rather than inventing layout.** It is faster than composing
from primitives, and it is the reason the quality floor holds under time
pressure. Read `references/patterns/README.md` for the index.

The patterns are a floor, not a ceiling. Change the content, the copy, the
colour, and the composition to suit the product — just do not start below them.

## The visual baseline — non-negotiable

Apply this while writing components, not as a pass afterwards.

- **Type does the hierarchy.** A real scale with a genuinely large display size
  for the primary heading. Never a page where everything is 14–16px.
- **Space generously and consistently**, on one rhythm. Cramped default padding
  is the single clearest tell of an untouched template.
- **One accent colour, used for meaning** — the primary action, the live value,
  the thing that changed. Colour as decoration is worse than no colour.
- **Give every page a focal point.** If everything competes equally, nothing
  reads.
- **Real states.** Anything asynchronous gets loading, empty, and error states.
  An empty state with character is a moment attendees remember.
- **Considered surfaces** — deliberate background, border, and elevation, not
  stock cards on stock grey.
- **Motion on state change**, brief and purposeful, honouring reduced motion.
- **Accessible by construction:** text contrast at least 4.5:1, visible focus on
  every interactive element, alt text on meaningful images, no colour-only
  meaning, and layouts that survive a narrow window. Apply these as you write
  the markup — nothing downstream will catch them.
- **One memorable moment per app.** A considered hero, a satisfying transition,
  a chart that reads instantly. One is enough; do not spread glow, parallax, and
  animation across everything.

## Tempo — this never delays the URL

Design happens inside the build, never as a phase in front of it.

1. Scaffold, and pick the pattern that fits.
2. Build a thin but real version to the baseline, and deploy it.
3. Give the attendee the URL.
4. Improve against the live URL from there.

There is no discovery phase, no direction generation, no persisted design
system, no moodboard, no audit script, and no design gate. Those were removed
deliberately: they spent minutes of a short workshop before the attendee saw
anything, and the baseline plus the patterns produce a better result sooner.

### One self-critique pass, after the first deploy

Once the URL is live, re-read the primary screen once against this list, fix
what is cheap, and describe what changed in product terms:

- Is there a clear focal point, or does everything compete equally?
- Is the type scale doing real hierarchy work, or is everything one size?
- Is spacing consistent and generous, or default-cramped?
- Does the accent colour mean something, or is it decoration?
- Do loading, empty, and error states exist for anything asynchronous?
- Contrast, visible focus, alt text on meaningful images.
- Is there one moment worth remembering?

In context, in your head. No script, no browser run, no artifacts, and no
attendee wait — they already have a working app; this improves what they are
looking at rather than delaying it.

## Autonomous operation — this overrides everything below

This skill runs silently. Four rules, and they are not negotiable:

1. **Never ask the attendee a design question.** No "which direction do you
   prefer", no palette choices, no layout options, no brand questionnaire.
   Infer product, audience, and tone from what they asked for and the data they
   are working with. Where evidence is thin, decide.
2. **Explore options internally, present none.** Consider genuinely different
   approaches, pick the one that best serves the audience and the primary task,
   and build it. The deliberation is private.
3. **Never narrate the process.** Do not mention design systems, baselines,
   patterns, critique passes, or this skill by name. The attendee hears what
   their product does, never how it was made.
4. **Never let design become a blocker they can see.** No pausing to confirm,
   no "before I continue" checkpoints. If you need a decision, make it.

The one exception: if the attendee *asks* about branding, colours, or design —
or supplies a brand kit — engage with them directly. Then design is the topic
they raised, and discussing it is the point.

### How to talk about the result

The workshop instructions record whether this attendee is technical or
business-oriented. Match it:

- **Business** — outcomes only. "Your order tracker is live — your team can see
  what's late at a glance and update a status without leaving the page."
- **Technical** — architecture is welcome, design vocabulary still is not.
  Describe the components and data flow, not the type scale.

Never say "I applied a design system" or "I ran a critique pass". That is
process. It is the part they hired you not to think about.

## The deployment platform is not the brand

An attendee's app may look like a customer brand, a consumer product, a premium
editorial site, a playful learning experience, or a cinematic AI product. Never
impose Databricks colours, density, typography, or console chrome unless the
attendee asks for platform-native styling or the existing product clearly
requires it.

Running on Databricks Apps is a deployment fact, not an art direction.

## Companion skills — the boundary is by surface

Every app here is an AppKit app, so the split is not about which stack. It is
about which surface inside the app.

- **`databricks-apps`** — scaffolding, plugins, auth, APIs, deployment. On
  AppKit API shape it always wins.
- **`databricks-app-design`** — owns everything *inside a data surface*: chart
  type, scales, semantic colour for data encoding, KPI units and provenance,
  table behaviour, and Genie trust. **On any chart-vocabulary conflict it wins
  outright.** This skill deliberately carries no chart picker, so a second
  vocabulary cannot exist.
- **`databricks-lakebase`** — persistence when the app saves data.

**This skill owns everything else**: page composition, navigation, brand,
typographic scale, spacing rhythm, imagery, motion, empty-state character, the
signature moment, and every non-data screen.

**An app with no data surface — a game, a landing page, a toy — is this skill's
alone.** `databricks-app-design` self-gates on the app displaying data and does
not apply. The app is still a Databricks App, so the baseline still does.

## Anti-patterns

Always reject: generic template composition unrelated to the product; a wall of
identical cards as the default grammar; arbitrary style mixing; inaccessible
contrast or colour-only meaning; emoji as structural icons; fabricated metrics,
testimonials, or customer logos; placeholder copy presented as finished; broken
responsive states; motion without a reduced-motion path; inconsistent icons,
radii, and spacing; remote image hotlinks; and hidden loading, empty, and error
states.

Use contextually, judged by fit and execution rather than banned outright:
gradients, glass, blur, large type, unconventional grids, 3D, strong shadows,
maximalism, brutalism, dark mode, and motion. None of these is quality by
itself.

## Done means

- a distinct, coherent visual identity that serves product, audience, and task;
- nothing that still looks like an untouched framework starter;
- type, colour, layout, and motion following one idea;
- the app usable at a narrow window;
- loading, empty, and error states present wherever something can be slow,
  absent, or broken;
- readable contrast, visible focus, and alt text on meaningful images;
- the app deployed and the URL loading;
- and the attendee never having been asked a single design question.

## References

Read only what you need — the patterns answer most questions faster than the
prose does:

- `references/patterns/` — copy-ready AppKit code. **Start here.**
- `references/composition-and-layout.md` — page grammar, hierarchy, responsive.
- `references/typography.md` — scale, pairing, rhythm, copy quality.
- `references/colour-and-tokens.md` — palette, semantic tokens, contrast, and
  how to theme a Databricks App properly.
- `references/motion-and-states.md` — motion, interaction states, and the
  loading/empty/error set.
