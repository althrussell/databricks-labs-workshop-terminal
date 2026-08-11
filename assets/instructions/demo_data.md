<!-- workshop-demo-data -->
## Demo data you already have

There is realistic, ready-to-query data in this workspace. **Use it.** Generating
fake data is slower, worse, and costs the attendee the part of the hour they came
for.

Reach for it whenever you need data and the attendee has not given you their own.
Before you write a single row of synthetic data, check here first.

{manifest}

### Rules

**It is read-only.** Everyone in this room reads the same tables. You cannot
create, insert, update or drop anything in this catalog — attempts will fail, and
would be wrong even if they succeeded.

**To write, clone it first.** One statement, and the copy is the attendee's to
do anything with:

```sql
CREATE TABLE {workshop_catalog}.<schema>.<table>
DEEP CLONE <demo_table>;
```

Deep clone, not `CREATE TABLE AS SELECT`: it is faster, it keeps the history, and
it does not depend on a warehouse chewing through the source. Clone only the
tables the attendee actually needs.

**Read it in place for anything read-only.** Dashboards, Genie spaces, queries and
notebooks can point straight at the demo catalog. Cloning is for when something
needs to be written to.

**Say what it is, once.** It is synthetic — realistic shapes, real-looking names,
entirely generated. Mention that the first time it comes up and then get on with
building. Do not caveat every chart.

**Read the comments before you guess.** Every table and column carries a comment
explaining what it holds. `DESCRIBE TABLE EXTENDED <table>` is one call and will
usually stop you from inventing a join that does not hold.

**Prefer the wide tables where they exist.** Names ending in `360` are already
joined and aggregated — one row per vehicle, per customer, per part, with the
metrics people actually ask for. Build on those rather than reassembling them
from the base tables, unless the attendee specifically wants to see how they are
put together.
