---
name: promote
description: Use when a build or deployment has completed to generate professional handoff documents (architecture, security, Jira stories, test cases, build prompt) and upload them to the attendee's Databricks Volume.
argument-hint: "[brief description of what you built]"
---

# Promote — Session Handoff Documents

Generate professional documentation from the current session and upload to the attendee's
Databricks Volume.

## When to use

Invoke after a successful build or deployment. When a build completes, always offer:

> "Your build is live! Want me to generate handoff docs — architecture, security review,
> Jira stories, test cases, and a build prompt — uploaded to your Databricks Volume?"

## Steps

### 1. Get description

Use the argument if provided. Otherwise ask:
> "What did you build? One sentence — I'll use it to ground the documents."

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

Write each generated document to `/tmp/promote/`:

```sh
mkdir -p /tmp/promote
```

Generate in this order (each later document can reference decisions in earlier ones):
1. `architecture.md` — conceptual and detailed architecture
2. `security.md` — threat model, IAM, data security, risk register
3. `jira-stories.md` — epics, stories, acceptance criteria
4. `test-cases.md` — test strategy, traceability, test cases
5. `build-prompt.md` — self-contained rebuild prompt

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
  databricks files upload "/tmp/promote/${doc}.md" "${PROMOTE_PATH}/${doc}.md" --overwrite \
    && echo "✓ ${doc}.md" \
    || echo "✗ ${doc}.md (upload failed — check credentials)"
done
```

### 5. Report results

Tell the attendee:
- The full Volume path (find files in Databricks Catalog explorer under Volumes)
- Which docs were uploaded successfully
- Any that failed and why

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
- Temp files in /tmp/promote clean up on next system restart.
