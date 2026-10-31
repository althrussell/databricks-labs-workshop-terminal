<!-- workshop-terminal-safety:v1 -->
**Keep this terminal running.** Port 8000 is the Workshop Terminal; stopping it
ends the session.

- Never stop, kill or restart a process you did not start: no `pkill`, `killall`,
  `kill` or `fuser -k` on python, uvicorn or node.
- Test-run apps only on `$DATABRICKS_APP_PORT`. It is set for you and is never
  8000. If it's busy, stop only the server you started there.
