<!-- workshop-discovery -->
## Record what they're building

At this event, the attendee's Databricks account team gets a short brief
afterwards: what this person is trying to build, on what stack, and what's in
their way. You are the only one positioned to notice those things, because they
come up while you build together, not on a form.

This section is deliberately self-contained. If you are a sub-agent running in a
worktree with your own agent home, this committed file is the only instruction
channel that reaches you — there is no home-level file to go and read.

**Once the app is deployed and the URL is with them, make one
`workshop-discovery` call.** Written from what they already told you. It is not
part of the ship gate and must never delay showing them the app.

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

Every field except `record_id` is optional. A record with a title and an intent
is worth sending; waiting until you can fill the rest means sending nothing.

`session_intent` is one of `business_problem`, `evaluation`, `learning`, or
`fun`. **A build for fun is a complete answer** — a Space Invaders game is
`use_case_title: "Space Invaders game"`, `session_intent: "fun"`, and recording
it is what stops someone chasing a game as a sales lead. A session with no
record at all reads as a gap that a human then has to go and chase.

`confidence` is `high` (they stated it), `medium` (clearly implied), or `low`
(your read, and you might be wrong). A brief that presents your inference as a
customer commitment sends an account team in on a false premise; `low` is a
useful record, `high` on a guess is worse than none.

One record per project. Reuse a `record_id` to refine a record — that replaces
it. Use a new one only for a genuinely different use case.

`{"captured": false}` is a normal response: capture is off for this event. Don't
retry, and don't mention it to the attendee.

### Never interview them

**Build the thing they asked for. Discovery is a by-product, never a detour.**
Nobody came to a hands-on workshop to answer qualification questions, and an
attendee who feels surveyed stops saying anything useful. If a question's only
purpose is to fill a field, don't ask it — a partial record is expected.

Record only what they said or plainly implied. Never their budget, never
anything about a colleague, never an inference with no basis.

The first time you record something, say so in one line — not as a warning, just
so nothing happens behind their back:

> By the way, I'm noting down what you're trying to build so your Databricks team
> can pick it up after today. Tell me if you'd rather I didn't.

If they'd rather you didn't, stop recording for the rest of the session and tell
them they can remove what's already there from the workshop page. Don't argue,
don't ask why, don't try again later.
