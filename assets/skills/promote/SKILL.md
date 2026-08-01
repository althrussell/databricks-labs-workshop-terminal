---
name: promote
description: Use when a build or deployment has completed, or when the workshop reaches its wrap phase, to generate professional handoff documents (architecture, security, Jira stories, test cases, build prompt) and upload them to the attendee's Databricks Volume.
argument-hint: "[brief description of what you built or explored]"
---

# Promote — Session Handoff Documents

Generate professional documentation from the current session and upload to the attendee's
Databricks Volume.

## When to use

Three triggers, and the first two are unconditional:

1. **A build or deployment completed.** Offer it, then run it if the attendee agrees:

   > "Your build is live! Want me to generate handoff docs — architecture, security review,
   > Jira stories, test cases, and a build prompt — uploaded to your Databricks Volume?"

2. **The workshop reaches its wrap phase**, or the attendee says they're finishing up.
   **Run it without asking, and run it whether or not anything shipped.** Say what you're
   doing in one line; don't make it a question. The environment is deleted after the
   workshop, so the wrap moment is the last chance to write anything down — waiting for a
   "yes" is how a session ends with nothing to take home.

3. **The attendee asks** (`/promote`, or "generate my docs").

Never skip this because the session looks unimpressive. A session that ran into a wall
produces the most useful document of the set — see "Nothing shipped" below.

## Steps

### 1. Get description

Use the argument if provided. Otherwise use what you already know from the session — you
have the full conversation, so infer it rather than interrogating someone who is packing
up. Only ask if the session is genuinely ambiguous, and ask once:
> "One sentence on what you were going for? I'll use it to ground the documents."

### 2. Generate each document

For each document type below, **first read the corresponding spec file in this skill's
directory**, then generate the document following that spec exactly. The spec files contain
the persona, required sections, and formatting rules for each document type.

```
Spec files (in this skill's directory, alongside this SKILL.md):
  architecture.md   → Enterprise Architect spec
  security.md       → CISO / Security Architect spec
  jira-stories.md   → Product Manager spec
  test-cases.md     → QA Engineer spec
  build-prompt.md   → Prompt Engineer spec
```

Write each generated document to `~/promote/`:

```sh
mkdir -p ~/promote
```

Use the home directory, not `/tmp`. `/tmp` is shared by every attendee on the container
and is cleared on restart, so a doc written there can be lost or attributed to the wrong
person.

**If `~/promote/<doc>.md` already exists from an earlier run in this session, update it
rather than starting over** — the earlier version was generated when there was less to
describe, and regenerating from scratch loses the decisions recorded in it.

Generate in this order (each later document can reference decisions in earlier ones):
1. `architecture.md` — conceptual and detailed architecture
2. `security.md` — threat model, IAM, data security, risk register
3. `jira-stories.md` — epics, stories, acceptance criteria
4. `test-cases.md` — test strategy, traceability, test cases
5. `build-prompt.md` — self-contained rebuild prompt

### 2b. Nothing shipped

If the session produced no working build — the attendee explored, hit a wall, or ran out
of time — still write the set. Change what goes in it, not whether it exists:

- `architecture.md` — the architecture they were **heading towards**, plus a
  "Where this stopped" section naming the specific blocker (the error, the missing grant,
  the unavailable connector) and what would unblock it.
- `security.md`, `jira-stories.md`, `test-cases.md` — for the intended design. A backlog
  for something not yet built is normal; that is what a backlog is.
- `build-prompt.md` — a prompt that would build it from scratch, including the workaround
  for whatever blocked them if you found one.

Do not describe an unfinished session as complete, and do not invent a deployment that
did not happen. State plainly what exists and what does not. An honest "we got to the
ingest and the broker auth failed" is worth more to the attendee — and to whoever helps
them next — than a document implying a finished system.

### 3. Resolve Volume path

```sh
CATALOG="${WORKSHOP_CATALOG:-main}"
SCHEMA="${WORKSHOP_SCHEMA:-default}"
USER_EMAIL=$(databricks current-user me --output json 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('userName','unknown'))" \
  2>/dev/null || echo "unknown")
PROMOTE_PATH="/Volumes/${CATALOG}/${SCHEMA}/promote/${USER_EMAIL}/$(date +%Y%m%d-%H%M%S)"
echo "Uploading to: $PROMOTE_PATH"
```

### 4. Upload to Volume

```sh
for doc in architecture security jira-stories test-cases build-prompt; do
  databricks files upload "$HOME/promote/${doc}.md" "${PROMOTE_PATH}/${doc}.md" --overwrite \
    && echo "✓ ${doc}.md" \
    || echo "✗ ${doc}.md (upload failed — check credentials)"
done
```

If the upload fails, **do not delete the local copies and do not treat the run as
failed** — the documents in `~/promote/` are the real output. Report the upload error and
move on.

### 5. Report results

Tell the attendee:
- The full Volume path (find files in Databricks Catalog explorer under Volumes)
- Which docs were uploaded successfully
- Any that failed and why
- That the Volume is dropped with the workshop catalog, so these are readable
  today but are not a take-home — to keep them, download them from the Catalog
  explorer or commit them into a repo they push to a remote they own

Example output:
```
✓ Docs uploaded to:
  /Volumes/main/default/promote/alice@example.com/20250630-142301/

  architecture.md   — Enterprise architecture spec
  security.md       — Security architecture review
  jira-stories.md   — Product backlog with epics and stories
  test-cases.md     — Test strategy and test cases
  build-prompt.md   — Self-contained rebuild prompt
```

## Notes

- Read each spec file before generating the corresponding document — the specs contain
  detailed personas, required sections, and formatting rules that determine output quality.
- Generate from the actual conversation history — no generic filler, no placeholders.
- If a file fails to upload, note the error and continue; do not abort remaining docs.
- Run this at wrap even if you already ran it earlier after a build. Update the existing
  documents in place; the session moved on since then.
