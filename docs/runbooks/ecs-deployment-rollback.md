# ECS Service Deployment Rollback

Applies when an ECS service degrades and a recent deployment is the leading
suspect. Rolling back is reversible and fast; prefer it over forward fixes while
an incident is active.

## Symptoms

- `runningCount` is below `desiredCount` and `pendingCount` is non-zero.
- Tasks stop with `Essential container in task exited` or
  `Task failed ELB health checks`.
- Error rate or latency stepped up rather than drifting, and the step lines up
  with a deployment timestamp.

## Diagnosis

- List recent deployments and note the revision and what changed.
- Compare the current task definition against the previous revision; a config or
  environment-variable change is as capable of breaking a service as a code change.
- Check whether tasks are failing to start at all versus starting and then failing
  health checks — the first points at image or configuration, the second at the
  application or its dependencies.
- Confirm the previous revision was healthy in production, not merely older.

## Mitigation

- Roll back to the last known-good revision:
  `aws ecs update-service --cluster <cluster> --service <service> --task-definition <family>:<previous>`
- Watch `runningCount` converge on `desiredCount` before making any other change.
- Do not scale the service up to mask a rollout failure; more failing tasks is
  not fewer failing tasks.

## Rollback

- Rolling forward again is the rollback of a rollback: re-deploy the newer
  revision only after the underlying defect is understood and fixed.

## Escalation

- Escalate to the owning team if `runningCount` does not recover within two task
  cycles after the rollback, which suggests the deployment was correlated with
  the incident rather than the cause of it.
