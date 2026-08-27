# Attendee messaging: the tab rule

One instruction decides whether a room of attendees hits a wall an hour in:

> **Leave the Workshop Terminal tab open.**

This page is what to say, and where the product says it for you.

---

## Why it is a rule and not a preference

An attendee's Databricks sign-in is bound to their browser tab. Workshop
Terminal never receives a refresh token — the Apps proxy hands it a short-lived
access token on each request the tab makes, and that is the only way it ever
gets a fresh one. The Omnigent host in their container runs off a mirror of that
token.

Close the tab, and the mirror ages out with nothing able to renew it. Every
Omnigent agent then fails, with a message about a terminal that would not start.
Nothing about that error suggests "your tab was closed", which is why attendees
who hit it ask an operator instead of fixing it themselves.

Bare Claude and Codex are unaffected: they run on the app's own
credential, which the server rotates regardless of who is looking at what.

---

## 1. The floor script

Say this during the intro, right before the first launch. Thirty seconds.

> "One housekeeping thing that will save you a headache. Keep this Workshop
> Terminal tab open for the whole session — you can switch to other tabs, use
> other windows, all fine. Just do not close this one. Your Databricks sign-in
> lives in this tab, and if it closes, the Omnigent agents lose their
> credentials and stop working.
>
> If that does happen: reload the tab. That is the whole fix, it takes a second,
> and you will not lose your work — your session is still running on the
> machine.
>
> And if an agent ever fails on you, Claude Code and Codex on the home
> screen always work. Grab one of those and flag me."

Repeat the one-liner after any break, when people have closed things down. That
is when it bites.

**Say it as a consequence, not a policy.** "Keep the tab open or the agents stop
working" is remembered; "please keep the tab open" is not.

**Do not** tell attendees to keep the tab *focused*. Background tabs are fine —
switching to the Databricks workspace, docs, or Slack costs nothing.

---

## 2. The persistent in-product notice

While this deployment has an Omnigent plane, a thin strip sits under the header
on every screen:

> Keep this tab open — it is what keeps your Databricks sign-in alive for
> Omnigent agents.

Nothing else — no clock. It is not dismissible, because people join late, take
breaks and forget, and the rule applies for the whole event. It carries no time
remaining either: renewal happens on its own, so a countdown gives an attendee
nothing to act on and reads like a failure in progress while they watch it.

Implemented in `frontend/src/components/SignInNotice.tsx`; hidden entirely on
deployments with no Omnigent plane, where the rule does not apply.

## 3. The blocking banner

The moment the sign-in actually goes stale, the strip is replaced by a
full-width banner in the danger colour with a **Reload** button:

> **Your Databricks sign-in has expired.** Reload this tab to sign in again — it
> takes a second and you will not lose any work. Claude and Codex keep
> working meanwhile.

Deliberately not a modal. Bare Claude and Codex still work at that
moment, and covering the screen would take away the one thing that does.

---

## What attendees should never be shown

- A harness code as a message (`spec_resolver_failed` and friends).
- Any suggestion to read runner logs — they cannot reach them. Omnigent's own
  wording is stripped before display (`stripDeadEnds` in
  `frontend/src/errors.ts`).
- A spinner for something that has already failed. A failed install step names
  the step instead.
- Advice to switch from one Omnigent harness to another. They share a credential
  plane and fail together; see [`operator-runbook.md`](./operator-runbook.md).
