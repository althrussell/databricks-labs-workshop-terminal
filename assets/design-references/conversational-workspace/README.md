# Conversational workspace

**Job:** ask questions of data or an agent, follow up, and keep the evidence for
each answer in view.

- The question box stays usable while an answer streams; a follow-up never
  needs a full-page scroll back to the top.
- Each answer shows its evidence (the query, table or document it came from)
  beside it; selecting an earlier answer brings its evidence back without
  losing the conversation.
- The agent says what it did and what it could not do. A refusal or a missing
  tool is shown as such, never papered over with a plausible answer.
- Long answers are readable: headings and short paragraphs, code and SQL in
  monospace with a copy action.
- Narrow layout: evidence moves below the selected answer as a disclosure.

States: `loading` (answer streaming, with a stop action), `empty` (suggested
first questions from the attendee's own data), `error` (the agent or model did
not answer; the question is kept to retry), `long-label`.

Checks: the conversation is a live region announced politely; the input keeps
focus after sending; keyboard can select earlier answers; focus visible; AA.
