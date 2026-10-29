<!-- workshop-demo-data -->
## Demo data in `$WORKSHOP_DEMO_CATALOG` (read-only)

Check these tables before generating synthetic data (`DESCRIBE TABLE EXTENDED <table>`; prefer `*360` tables where the schema has them). Mention once that it is synthetic. To write to a table, clone it into `{workshop_catalog}` first:

```sql
CREATE TABLE {workshop_catalog}.<schema>.<table> DEEP CLONE <demo_table>;
```

{manifest}
