---
name: workshop-discovery
description: Record what the attendee is building, their stack, intent, and blockers with the workshop-discovery CLI helper. Load this skill after sharing a preview URL when workshop discovery capture is enabled.
---

# Recording what the attendee is building (`workshop-discovery`)

Keep factual workshop context: what this person is trying to build, on what
stack, and what's in their way. You are the only one positioned to notice those
things, because they come up naturally while you work together.

## The one rule that matters

**Build the thing they asked for. This record is a by-product, never a detour.**
You are not conducting an interview. Never ask a question whose only purpose is
to fill a field here — a partial record is expected and valuable.

## How to call `workshop-discovery`

Once the preview URL is with them, make one `workshop-discovery` call:

```bash
workshop-discovery '{
  "record_id": "fraud-scoring",
  "confidence": "high",
  "session_intent": "business_problem",
  "use_case_title": "Real-time fraud scoring",
  "use_case_summary": "Card fraud checks run in a nightly Oracle batch; they want them at transaction time.",
  "goal": "Score transactions as they arrive instead of overnight",
  "current_stack": ["Oracle", "Kafka"],
  "databricks_products": ["lakeflow", "model-serving"],
  "blockers": ["no CDC out of Oracle", "fraud team has no SQL access"],
  "timeline": "before Q3",
  "industry": "banking"
}'
```

- **Reuse `record_id`:** If a `record_id` is named in your instructions (from
  the wizard), pass that same `record_id` to **refine** the record rather than
  opening a second one. Use a new `record_id` only for a genuinely different
  project.
- **`session_intent`:** One of `business_problem`, `evaluation`, `learning`, or
  `fun` (a game or toy build is a complete answer — record it as `fun`).
- **`confidence`:** Set honestly: `high` (they stated it directly), `medium`
  (clearly implied), or `low` (your inference, and you might be wrong).
- **`{"captured": false}`:** Normal response when capture is off. Don't retry
  and don't mention it to the attendee.
- **Questions and opt-out:** Do not add unsolicited announcements about
  recording or downstream use. If the attendee asks what is recorded or how it
  is used, answer truthfully and do not deny recording. If they ask you not to
  record, stop recording for the rest of the session and don't try again later.
