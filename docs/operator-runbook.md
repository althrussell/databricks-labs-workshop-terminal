# Operator runbook — the floor during an event

This is the page to have open while a workshop is running. It answers one
question: *an attendee says their agent is broken — what do I do?*

Read the ladder first. Everything else is detail.

---

## The one thing that is easy to get wrong

**Omnigent's Claude and Omnigent's Codex are not a fallback.**

Every harness inside Omnigent — the TUI, `omnigent claude`, `omnigent codex`,
`pi`, every polly worker — reaches the workshop through one runner process with
one credential (`_RunnerDatabricksAuth`, built once in the runner's entrypoint).
When that credential is stale, the runner fails *before* it gets as far as
choosing a harness. Pi surfaced it first at our last event only because native
terminals make one extra launch-config call; Claude and Codex through Omnigent
die the same way, one step later.

Sending an attendee from Omnigent's Pi to Omnigent's Claude is sending them in a
circle. It will look like a second, unrelated bug.

**The real fallback is the bare tier: Claude Code and Codex.** Those
run directly in the attendee's container on the app service principal's
credential, which the server rotates on its own schedule and which no browser
tab can invalidate. They cannot fail the way the Omnigent plane fails. The
product enforces this: `launch_block` in `server/agents.py` consults credential
state *only* for Omnigent-backed cards, so bare cards stay launchable even when
everything else is gated off.

The model-comparison exercise is the exception: it no longer has a bare-tier
form. Those models answer only on chat-completions, and codex-cli dropped that
wire, so there is no Codex profile behind them any more. The endpoints
themselves are still live — `/api/config` publishes the URL under
`model_comparison` — but reaching them means asking a bare agent to make the
HTTP call. See [`model-comparison.md`](./model-comparison.md).

---

## The ladder

Work down it. Each rung is roughly what it costs the attendee.

### Rung 1 — Reload the tab (~15 seconds)

**When:** the attendee sees "your Databricks sign-in went stale", or an Omnigent
card says *reload to sign in*.

Ask them to reload the Workshop Terminal tab and start a new session.

Their sign-in token is bound to that browser tab: the app only sees a fresh one
when the tab makes a request. A reload hands over a new token, the server
re-mirrors it for the Omnigent host within a second, and the next launch works.
This fixes the large majority of reports.

If the tab was closed, reopening it is the same fix.

### Rung 2 — Move them to the bare tier (~30 seconds)

**When:** rung 1 did not take, or the attendee is mid-task and cannot afford to
retry.

Tell them to use **Claude Code** or **Codex**. Both are on
the same home screen and need no sign-in of their own. If they were doing the
model-comparison exercise, move them to a different exercise rather than a
different harness — there is no bare-tier form of that one to fall back to (see
above), and offering a `codex --profile` for it will cost them a minute finding
out it does not start.

This is a real answer, not a consolation prize. The attendee keeps working while
you deal with the cause.

### Rung 3 — Recover, from the operator panel (~2 minutes)

**When:** a reload did not help, or several attendees report the same thing.

Operator panel → **Omnigent plane** → **Recover** on the attendee's row (or
**Recover everyone**).

Recover runs the three steps the server already takes on its own: re-mirror the
attendee's token, wake or restart their Omnigent host, and ask their tab for a
fresh token. It reports back per attendee — if the row still says *stale — tab
closed or expired*, the attendee's tab is genuinely not open and signed in, and
no amount of recovering will change that. Send them to rung 1.

The same button exists for the attendee, on the error banner itself. Them
pressing it is also your earliest signal that somebody is stuck.

API equivalent, for a fleet script:

```bash
curl -sX POST "$WT/api/admin/recover" -H 'content-type: application/json' -d '{}'
```

### Rung 4 — Demote Omnigent, fleet-wide

**When:** the Omnigent plane is failing across a room and rungs 1–3 are not
holding. Typically: many attendees, same error, recovery not sticking.

Operator panel → **Omnigent plane** → **Demote Omnigent (fleet)**.

Every Omnigent-backed card is withdrawn for everyone, immediately — open tabs
are pushed the change, they do not need to reload. Claude and Codex
stay exactly as they were. Attendees see *paused by your host* on the withdrawn
cards rather than an error they cannot act on.

This is deliberately not the spend kill-switch. The kill-switch pauses *all*
agents and stops the workshop; demoting keeps it running on the tier that cannot
fail this way. Press **Restore Omnigent** when the cause is fixed.

```bash
curl -sX POST "$WT/api/admin/omnigent-tier" \
  -H 'content-type: application/json' -d '{"enabled": false}'
```

---

## What not to do

**Do not restart the `wt` or `omni` app as a first move.** A restart kills every
attendee's live session and their in-progress work, and it does not refresh
anybody's browser-bound sign-in — the thing that is usually wrong. Rungs 1–4 do
not lose any work. A restart is what you do after the event, or when the
container itself is unhealthy and you have already accepted the cost.

**Never ask an attendee to read runner logs.** They have no access to them, and
Omnigent's own error text suggesting it is stripped before it reaches their
screen for exactly that reason. You read the logs; see below.

**Do not send an attendee from one Omnigent harness to another.** See the top of
this page.

---

## Seeing what the attendee saw

You do not need their browser, and you should not need their description.

| What you want | Where |
|---|---|
| Recent classified errors, per attendee, with the underlying traceback | `GET /api/admin/diagnostics` |
| Redacted tail of an attendee's runner / host process logs | `GET /api/admin/diagnostics/logs?attendee=…&source=runner` |
| Force a collection now rather than waiting for the sweep | `POST /api/admin/diagnostics/sweep` |
| Whether this instance is fit to run at all | `GET /readyz` |
| From your laptop, across the fleet | `scripts/pull_diagnostics.py` |

Codes are shared vocabulary: what the attendee is shown and what you see are the
same event. `native_terminal_start_failed`, `spec_resolver_failed`, `obo_stale`
and `runner_disconnected` all mean the same underlying thing — the attendee's
sign-in went stale — and all four are answered by rungs 1–3.

Credentials never appear in any of these surfaces, and raw terminal scrollback
never leaves the attendee's container.

---

## Before the room fills

- `/readyz` green on every instance. It goes red on its own if a credential will
  not last the remaining event duration, so trust it over a spot check.
- Operator panel → Credential health reads *rotating*, not *degraded*.
- Omnigent plane table: every attendee row has a sign-in with time left on it.
  Rows reading *never captured* are attendees who have not opened their tab yet
  — expected before the event, a problem during it.
- Tell the room the tab rule during the intro: **leave the Workshop Terminal tab
  open**. It is what keeps their sign-in alive. See
  [`attendee-messaging.md`](./attendee-messaging.md).

---

## Related

- [`auth-identity-model.md`](./auth-identity-model.md) — why the credential is
  tab-bound and what each plane can do.
- [`admin-api.md`](./admin-api.md) — the full operator API.
- [`model-comparison.md`](./model-comparison.md) — the Pi-free comparison
  exercise.
- [`control-tower-implementation.md`](./control-tower-implementation.md) —
  fleet-level operations and admission.
