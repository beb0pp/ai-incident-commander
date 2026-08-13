# Database Connection Pool Exhaustion

Applies to services backed by Aurora PostgreSQL where clients cannot acquire a
connection. The database is usually healthy; the ceiling is.

## Symptoms

- Application errors read `timeout acquiring connection from pool` or
  `remaining connection slots are reserved`, not query errors.
- `DatabaseConnections` sits at or just under `max_connections`.
- CPU on the database is unremarkable. Write latency may climb as sessions queue.
- Errors often begin within minutes of a deployment that changed pool sizing,
  task count, or instance class.

## Diagnosis

- Read the current connection count against the instance's `max_connections`:
  `aws rds describe-db-instances --db-instance-identifier <identifier>`.
- Compute the theoretical ceiling: per-task pool size multiplied by running task
  count, summed across every service sharing the instance. Compare it to
  `max_connections`. If the theoretical ceiling exceeds the limit, the incident is
  a capacity arithmetic error, not a leak.
- Check for a recent deployment that changed the pool size or the desired count.
- Identify idle-in-transaction sessions:
  `SELECT pid, state, age(now(), state_change) FROM pg_stat_activity ORDER BY 3 DESC;`
- Rule out a genuine leak: if connection count grows monotonically with no
  corresponding traffic increase, treat it as a leak and go to escalation.

## Mitigation

- Prefer rolling back the change that altered the arithmetic. It is the smallest
  reversible action and it restores a known-good state.
- If no deploy is implicated, reduce the per-task pool size and redeploy; do not
  raise `max_connections` as a first response.
- Terminate sessions idle in transaction for longer than five minutes only after
  confirming with the owning team.
- Raising the instance class increases `max_connections` but requires a restart
  and is a last resort during an active incident.

## Rollback

- Re-deploy the previous task definition revision:
  `aws ecs update-service --cluster <cluster> --service <service> --task-definition <family>:<previous>`
- Confirm the connection count falls below 80% of `max_connections` before
  declaring the incident mitigated.

## Escalation

- Escalate to the database on-call if connection count stays at the ceiling after
  the rollback completes and tasks have cycled — that indicates a leak rather
  than a sizing error.
