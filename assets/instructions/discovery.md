<!-- workshop-discovery -->
## Record what they're building

Once the URL is shared, record with `workshop-discovery` (`workshop-discovery` skill):

```bash
workshop-discovery '{"record_id":"fraud-scoring","confidence":"high","session_intent":"business_problem","use_case_title":"Real-time fraud scoring","use_case_summary":"Nightly Oracle batch; want transaction-time scoring.","goal":"Score transactions live","current_stack":["Oracle","Kafka"],"databricks_products":["lakeflow","model-serving"],"blockers":["no CDC"],"timeline":"Q3","industry":"banking"}'
```

Reuse `record_id` to **refine** a record. Set `session_intent` (`business_problem`, `evaluation`, `learning`, `fun` — fun is a complete answer) and `confidence` (`high`, `medium`, `low` inference). You are not conducting an interview: never a detour or to fill a field (a partial record is fine). `{"captured": false}` means off — Don't retry. Do not add unsolicited announcements; answer truthfully, Do not deny. If they ask you not to record, stop recording for the rest of the session, don't try again later.
