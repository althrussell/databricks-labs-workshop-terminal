<!-- workshop-demo-data -->
## Demo data in `$WORKSHOP_DEMO_CATALOG` (read-only)

Check these tables before generating synthetic data (`DESCRIBE TABLE EXTENDED <table>`; prefer `*360` tables where the schema has them). Mention once that it is synthetic. Query them where they are and join them with tables in `{workshop_catalog}`; write new derived tables there, never copies made only to read.

The data covers a fixed period, not up to today. Before answering anything relative to now ("last week", "this month"), check the range (`MIN`/`MAX`) of the date column the question is about; a table can have several, and some (expiry or due dates) run into the future. Anchor the period to the latest date of activity in that column and say which dates you used, in the attendee's words. An app built on it does the same. Never return an empty answer for a period the data doesn't cover without saying why.

{manifest}
