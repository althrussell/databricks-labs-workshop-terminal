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

Write each document from the persona and requirements below, using the full conversation history and the attendee's description as the source of truth. Every document must reflect what was **actually built** — not a generic template.

---

**architecture.md**
*You are a software architect. Write clear, detailed technical architecture documentation.*

Write a detailed architecture document for the application described in this session. Include:
- System overview (one paragraph)
- Component diagram as ASCII art or a structured list
- Data flow, step by step from user input to output
- Technology choices and rationale (why AppKit, why Lakebase, why serverless, etc.)
- Scalability considerations
- Databricks-specific components used — serving endpoints, Unity Catalog, Lakebase, Apps, Jobs, pipelines — whatever was built

Format as a markdown document.

---

**security.md**
*You are a security engineer. Write thorough, practical security documentation.*

Write security documentation for the application described in this session. Include:
- Authentication and authorisation design (Databricks Apps SSO, service principal, OBO token flow, Unity Catalog grants)
- Data classification and handling (what data the app touches, sensitivity level)
- Network security (how the app is exposed, private link, ingress boundaries)
- Secrets management (where credentials live, rotation, scoping)
- Compliance considerations relevant to the data and use case
- Databricks security controls applied (row filters, column masks, Unity Catalog permissions, audit logging)

Format as a markdown document.

---

**jira_stories.md**
*You are a product manager. Write well-structured Jira stories.*

Write the full set of Jira stories required to build the application described in this session, ordered by execution sequence. For each story include:
- Title as `## US-NNN: <title>`
- Description
- Acceptance criteria as a bullet list of verifiable conditions
- Story points (choose from: 1, 2, 3, 5, 8)
- Dependencies on other stories (by US-NNN reference)

Format as markdown with each story as a level-2 heading.

---

**test_cases.md**
*You are a QA engineer. Write comprehensive test case documentation.*

Write comprehensive test case documentation for the application described in this session. Include:
- Unit tests (function-level, isolated)
- Integration tests (cross-component, including Databricks API calls)
- End-to-end test scenarios (full user journey from UI to data layer)
- Edge cases and failure scenarios
- Performance considerations

For each test case include:
- Test ID (TC-NNN)
- Description
- Preconditions
- Steps
- Expected result

Format as a markdown document.

---

**build_prompt.md**
*You are a prompt engineer. Write a complete, self-contained AI prompt.*

Write a single comprehensive prompt that a developer could paste into an AI assistant to build the entire application described in this session from scratch. The prompt must be fully self-contained — no external context required. Include:
- What the app does and who it's for
- Full tech stack (AppKit React + Vite + TypeScript frontend, Python backend, Databricks Apps deployment)
- Data models and schema
- API contracts (endpoints, request/response shapes)
- UI design decisions and layout
- Deployment target (Databricks Apps) and environment variables needed
- Any constraints or conventions to follow

Write ONLY the prompt text — no preamble, no explanation, no framing sentences.

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
