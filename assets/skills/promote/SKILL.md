---
name: promote
description: Use ONLY when the attendee explicitly asks for handoff documentation — /promote, "generate my docs", "write me an architecture doc", or tapping the workshop's docs suggestion card. Generates architecture, security, Jira stories, test cases, and a build prompt, and uploads them to the attendee's Databricks Volume. Never run this unprompted.
argument-hint: "[brief description of what you built or explored]"
---

# Promote — Session Handoff Documents

Generate professional documentation from the current session and upload to the attendee's
Databricks Volume.

## When to use

**One trigger: the attendee asked.** `/promote`, "generate my docs", "write me an
architecture doc", or tapping the documentation card the workshop UI already shows them.

**Never run this unprompted.** Not after a successful build, not at the wrap phase, not
because the session is ending. The attendee came to build something, and answering a
working app with a pitch for paperwork spends the one thing the workshop is short of.
The workshop UI already offers documentation at the moments it makes sense; that offer is
theirs to take or ignore.

**Never pitch it either.** Do not end a build with "want me to generate handoff docs?".
If they want documentation they will ask, and they have a button.

Once they *have* asked, generate the full set and do it properly. A session that ran into
a wall produces the most useful document of the set — see "Nothing shipped" below — so an
unimpressive-looking session is no reason to hold back on a pack they requested.

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

### 2c. Capture the design record

The visual decisions live in the code, not in a separate design folder — theme
tokens, the type scale, the accent colour, spacing, and the component patterns
the app was built from. That reasoning was deliberately kept out of the
conversation, so if `build-prompt.md` does not carry it, it is lost when the
environment is deleted and a rebuild produces a default-styled approximation.

Read the actual values out of the project — the theme or token file, the global
stylesheet, the shell component — and record them concretely in
`build-prompt.md`: the palette with its accent and what that accent signifies,
the type scale and font stacks, the spacing rhythm, the layout structure, the
motion treatment, and the app's one memorable moment. Reference the same
decisions from `architecture.md`.

Write them as requirements, not suggestions. Do not turn this into a design
lecture for the attendee — it is documentation, delivered with the rest of the
pack.

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
- If the attendee asks again later in the session, update the existing documents in place
  rather than regenerating from scratch; the session moved on since then.
