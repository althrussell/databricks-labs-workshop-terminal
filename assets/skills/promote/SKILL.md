---
name: promote
description: Use after completing a build to generate professional handoff documents (architecture, security review, Jira stories, test cases, build prompt) and upload them to the attendee's Databricks Volume.
argument-hint: "[brief description of what you built]"
---

# Promote — Session Handoff Documents

Generate professional documentation from the current session and upload it to the attendee's Databricks Volume.

## When to use this skill

Use **promote** proactively at the end of a successful build. When you have deployed an app, pipeline, dashboard, or any other Databricks resource, offer the attendee a chance to generate handoff docs before the session ends:

> "Your build is done! Want me to generate handoff documentation — architecture spec, security review, Jira stories, test cases, and a build prompt — and upload it to your Databricks Volume? Just say yes."

## Steps

### 1. Get description

Use the argument if provided. Otherwise ask:
> "What did you build? Give me a one-sentence description — I'll use it to ground the documents."

### 2. Prepare temp directory

```sh
mkdir -p /tmp/promote
```

### 3. Generate each document

Generate the five documents below from the conversation history and the description. Write each as a separate markdown file using:

```sh
cat > /tmp/promote/<doc_id>.md << 'ENDDOC'
<generated content>
ENDDOC
```

**architecture.md** — High-level architecture spec:
- What the system does (one paragraph)
- Components and their responsibilities
- Databricks services used (Apps, Lakebase, Model Serving, Unity Catalog, Jobs, etc.)
- Data flow diagram (ASCII or list)
- Key dependencies and versions

**security.md** — Security review:
- Authentication mechanism (Databricks Apps SSO, service principal, OBO)
- Authorization model (who can access what, Unity Catalog grants, row/column filters)
- Data at rest and in transit
- Secrets management approach
- Identified risks and mitigations
- Recommendations

**jira_stories.md** — Sprint-ready user stories:
- 4–6 user stories in the format:
  ```
  ## US-001: <title>
  **As a** <persona>, **I want** <goal>, **so that** <outcome>.
  **Acceptance criteria:**
  - [ ] criterion 1
  - [ ] criterion 2
  **Story points:** <estimate>
  ```

**test_cases.md** — Test scenarios:
- Unit tests: function-level, input/expected output
- Integration tests: cross-component interactions
- End-to-end tests: user journeys from UI to data layer
- Each test: test name, preconditions, steps, expected outcome

**build_prompt.md** — Self-contained build prompt:
A single LLM prompt that fully recreates the project from scratch. Include:
- Context (what this is, why it exists)
- Tech stack (AppKit, Lakebase, Databricks services)
- Feature requirements
- Deployment instructions
- Any constraints or conventions

### 4. Resolve Volume path

```sh
CATALOG="${WORKSHOP_CATALOG:-main}"
SCHEMA="${WORKSHOP_SCHEMA:-default}"
USER_EMAIL=$(databricks current-user me --output json 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('userName','unknown'))" 2>/dev/null \
  || databricks workspace whoami --output json 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('userName','unknown'))" 2>/dev/null \
  || echo "unknown")
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
PROMOTE_PATH="/Volumes/${CATALOG}/${SCHEMA}/promote/${USER_EMAIL}/${TIMESTAMP}"
echo "Uploading to: $PROMOTE_PATH"
```

### 5. Upload to Volume

```sh
for doc in architecture security jira_stories test_cases build_prompt; do
  databricks files upload "/tmp/promote/${doc}.md" "${PROMOTE_PATH}/${doc}.md" --overwrite \
    && echo "✓ ${doc}.md" \
    || echo "✗ ${doc}.md (upload failed — check credentials)"
done
```

### 6. Report results

Tell the attendee:
- The full Volume path (so they can find the files in the Databricks Catalog explorer)
- Which docs were uploaded successfully
- Any that failed (and why, if known)

Example:
```
✓ Docs uploaded to:
  /Volumes/main/default/promote/alice@example.com/20250630-142301/

  architecture.md   — system design
  security.md       — security review
  jira_stories.md   — 5 user stories
  test_cases.md     — unit + integration + E2E scenarios
  build_prompt.md   — ready-to-use build prompt
```

## Notes

- If a file fails to upload, note the error and continue — do not abort the remaining docs.
- The Volume path uses the attendee's (`me`) identity, so Unity Catalog entitlements are respected.
- Temp files in `/tmp/promote/` are safe to leave — they clean up on next restart.
- Do not generate docs from scratch — ground them in the actual conversation history. Docs must reflect what was actually built, not a generic template.
