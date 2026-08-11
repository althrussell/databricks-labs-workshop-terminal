# Verification gate

Nothing ships to an event on assertion. This is the list of things that must
have been *run*, what each one proves, and what a failure means.

The incident that produced this gate looked like a Pi bug for a day and a half.
It was a tab-bound credential going stale, every Omnigent harness dying the
same way, and no operator able to see it. Each check below exists because some
part of that was invisible.

## 1. Tab closed for two hours

`tests/test_tab_closed.py`

Closes the tab (stops handing over fresh tokens, lets the mirror expire), waits
out the credential, then reopens it.

Passes when: Omnigent is withdrawn with `credential_stale` rather than failing
at launch, bare Claude / Codex / Terminal stay ready throughout, and reopening
the tab re-mirrors the token, wakes the host and restores the Omnigent cards
with no operator involvement.

A failure here means an attendee who went to lunch comes back to a broken
workshop and a support queue.

## 2. Eight-hour soak

`tests/test_soak_and_chaos.py` — compressed, runs in seconds
`scripts/soak.py watch` — the same guarantees against a live deployment

The test drives a controlled clock through eight hours of a tab polling every
minute: every renewal, every watcher sample, every gate decision. It asserts
there is no minute in which the credential is stale, that per-capture state
does not accumulate, and that the watcher asks for a renewal *before* the
credential is unusable rather than after.

The script does the same against a deployment and exits non-zero if the run
contained any window where an Omnigent launch would have failed:

```bash
export DATABRICKS_TOKEN=...          # admin principal, CAN_USE on the app
scripts/soak.py watch --url https://<app>.databricksapps.com --hours 8
scripts/soak.py watch --urls ./instances.txt --interval 300 --trace ./soak.jsonl
```

Run it overnight against the rehearsal fleet, not on event morning. The
`app credential low-water` line in the verdict is the evidence a server-side
renewal actually happened during the window — if it never dips, the soak was
shorter than the token lifetime and proves nothing about renewal.

## 3. Chaos

Four failures, each of which must self-recover or degrade into something the
product explains.

| Failure | Where it is forced | Required behaviour |
|---|---|---|
| Credential force-expired | test, clock advance past expiry | Omnigent withdrawn with `credential_stale`; recovery reports `credential_fresh: false` instead of pretending; the next live tab restores it |
| `auth_tokens.json` deleted | test, `unlink` | Recovery rewrites the file the host reads, without the attendee noticing |
| Host killed mid-turn | test, mocked notify | Recovery wakes the host rather than leaving it waiting behind a live mirror |
| Renewal never comes | test, two hours of unanswered samples | Omnigent refuses with a 503 the attendee can act on; bare CLIs still launch; exactly one `obo.health` transition, not a per-sample storm |

The two an operator can cause from outside the box are also runnable against a
deployment, because those are the levers they will actually reach for:

```bash
scripts/soak.py chaos --url https://<app>... --case recover
scripts/soak.py chaos --url https://<app>... --case demote-restore
```

Force-expiring a credential and deleting the mirror stay in the test suite —
doing either to a live instance means reaching inside the container, which is
the thing the diagnostics work exists to avoid.

## 4. Scale rehearsal

`scripts/rehearse.py` — the fleet, all at once
`tests/test_scale_rehearsal.py` — the gate that makes it enforceable

Two instances passing acceptance proves the contract. It does not prove the
fleet. Rehearsal asks every instance the same questions simultaneously and
reports by exception:

```bash
scripts/rehearse.py --urls ./instances.txt --event-hours 8
```

It fails the run if any instance has a red hard check, a diagnostics collector
that never started (a failure there would be invisible to the operator), a
static credential expiring inside the event window, or nothing renewing the
attendee credential. It also reports **release drift** — instances running a
different manifest from the majority — because a fleet where one instance
carries a different release is a fleet where one attendee has a different
workshop, discovered by them.

### The admission gate

`/readyz` carries a hard `credential_durability` check, so an instance that
cannot keep its credentials alive for the rest of the event can be refused an
attendee.

Blocking admission on it is
[Control Tower's item 9](control-tower-implementation.md), and CT now does it:
`await_terminal_admission` polls this endpoint after a terminal deploys and fails
a required unit that never clears, so a broken instance costs a seat at
provisioning rather than an attendee mid-exercise. Landed in
`databricks-labs-control-tower#89`.

What it blocks on is not the 200 — see
[the admission rule](control-tower-implementation.md#the-admission-rule) for why
`obo` is exempt and how the `attendee_dependent` flag carries that. A CT older
than #89 waits on the Apps deployment state alone, in which case every hard gate
here is a report an operator has to read rather than a gate.

It asks whether each plane can be **kept** alive, not whether a token outlives
the event:

- **App plane** — durable when rotating, or when a static token's expiry passes
  `WORKSHOP_EVENT_ENDS_AT`.
- **Attendee plane** — durable when the OBO freshness watcher is running.

That second one is the point. No attendee OBO survives an eight-hour event;
they are minted for about an hour. A gate built on raw expiry would be red on
every instance from the first minute and switched off after one event. What has
to outlast the event is the renewal loop, so that is what is checked. The two
failures it does catch are the ones that end a workshop mid-session: a static
token configured with an expiry inside the event window, and a deployment wired
to remote Omnigent with nothing renewing the attendee credential — the incident
this whole plan exists to prevent, expressed as a pre-flight check.

Set `WORKSHOP_EVENT_ENDS_AT` (epoch seconds) or the static-credential half of
the check has no window to judge against and passes by default.

## 5. Live identity check — the one thing no test can prove

Everything above runs without a workspace. This does not, and it is the check
that matters most: the Omnigent CLI-parity fix is a shell wrapper reacting to an
environment variable set by a host process, and only a real deployment has both.

Run it on a deployed instance, in an **Omnigent native terminal** (not a WT bash
session — WT was never the broken plane):

```bash
databricks current-user me      # expect the wt app service principal
databricks-me current-user me   # expect the attendee
```

Then prove the create path end to end, because resolving an identity and being
able to build with it are different claims:

```bash
databricks schemas create verify_$USER <workshop-catalog>
databricks schemas get <workshop-catalog>.verify_$USER   # note the owner
```

The schema is created by the service principal. Within one reconcile interval
the attendee must be able to use it — check with `databricks-me schemas get`,
which authenticates as them. If that fails, the reconciler is the problem, not
the wrapper, and `entitlements.health` will say so.

Delete the schema afterwards. The same evidence is available without a terminal
from the admin diagnostics identity snapshot, which records what each CLI
surface resolved to per plane at session start:

```bash
scripts/pull_diagnostics.py summary --url https://<app>.databricksapps.com
```

Identical rows across the `workshop_terminal` and `omnigent` planes is the
healthy state. `databricks` resolving to the attendee inside Omnigent means the
wrapper is not in effect and creates will 403.

See [operator-runbook.md](operator-runbook.md) for what to do when one of these
fails during an event, and [auth-identity-model.md](auth-identity-model.md) for
why the credential behaves this way.
